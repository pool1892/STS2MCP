using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Multiplayer.Game;

namespace STS2_MCP;

[ModInitializer("Initialize")]
public static partial class McpMod
{
    public const string Version = "0.4.0";
    public const int DefaultPort = 15526;
    private const string ConfigFileName = "STS2_MCP.conf";
    private const int MainThreadReadTimeoutMs = 5000;
    private const int MainThreadActionTimeoutMs = 10000;
    private const int RequestBodyTimeoutMs = 5000;
    private const int MaxRequestBodyChars = 64 * 1024;

    private static HttpListener? _listener;
    private static Thread? _serverThread;
    private static readonly ConcurrentQueue<Action> _mainThreadQueue = new();
    internal static readonly JsonSerializerOptions _jsonOptions = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping
    };

    private static int LoadPort()
    {
        try
        {
            string? modDir = Path.GetDirectoryName(
                System.Reflection.Assembly.GetExecutingAssembly().Location);
            if (modDir == null) return DefaultPort;

            string configPath = Path.Combine(modDir, ConfigFileName);
            if (!File.Exists(configPath))
            {
                try
                {
                    var defaultConfig = new Dictionary<string, object> { ["port"] = DefaultPort };
                    string json = JsonSerializer.Serialize(defaultConfig, _jsonOptions);
                    File.WriteAllText(configPath, json);
                    GD.Print($"[STS2 MCP] Created default config at {configPath}");
                }
                catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
                {
                    GD.Print($"[STS2 MCP] No config found at {configPath}; using default port {DefaultPort}");
                }
                return DefaultPort;
            }

            string content = File.ReadAllText(configPath);
            using var doc = JsonDocument.Parse(content);
            if (doc.RootElement.TryGetProperty("port", out var portElem)
                && portElem.TryGetInt32(out int port)
                && port is > 0 and <= 65535)
            {
                return port;
            }

            GD.PrintErr($"[STS2 MCP] Invalid or missing 'port' in {configPath}, using default {DefaultPort}");
            return DefaultPort;
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] Failed to load config: {ex.Message}, using default port {DefaultPort}");
            return DefaultPort;
        }
    }

    public static void Initialize()
    {
        try
        {
            // Optional settings UI patches should not block the HTTP bridge itself.
            TryApplyHarmonyPatches();

            // Connect to main thread process frame for action execution
            var tree = (SceneTree)Engine.GetMainLoop();
            tree.Connect(SceneTree.SignalName.ProcessFrame, Callable.From(ProcessMainThreadQueue));

            int port = LoadPort();

            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{port}/");
            _listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            _listener.Start();

            _serverThread = new Thread(ServerLoop)
            {
                IsBackground = true,
                Name = "STS2_MCP_Server"
            };
            _serverThread.Start();

            GD.Print($"[STS2 MCP] v{Version} server started on http://localhost:{port}/");
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] Failed to start: {ex}");
        }
    }

    private sealed class EndpointModeException : Exception
    {
        public EndpointModeException(string message) : base(message) { }
    }

    private static void TryApplyHarmonyPatches()
    {
        try
        {
            new Harmony("com.sts2mcp").PatchAll();
        }
        catch (Exception ex)
        {
            GD.Print(
                $"[STS2 MCP] Optional Harmony settings UI injection skipped: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private static void ProcessMainThreadQueue()
    {
        int processed = 0;
        while (_mainThreadQueue.TryDequeue(out var action) && processed < 10)
        {
            try { action(); }
            catch (Exception ex) { GD.PrintErr($"[STS2 MCP] Main thread action error: {ex}"); }
            processed++;
        }
    }

    internal static Task<T> RunOnMainThread<T>(Func<T> func)
    {
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);
        _mainThreadQueue.Enqueue(() =>
        {
            try { tcs.SetResult(func()); }
            catch (Exception ex) { tcs.SetException(ex); }
        });
        return tcs.Task;
    }

    internal static Task RunOnMainThread(Action action)
    {
        var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        _mainThreadQueue.Enqueue(() =>
        {
            try { action(); tcs.SetResult(true); }
            catch (Exception ex) { tcs.SetException(ex); }
        });
        return tcs.Task;
    }

    private static bool TryRunOnMainThread<T>(
        Func<T> func,
        int timeoutMs,
        out T result,
        out Exception? error,
        out bool mayStillBeRunning)
    {
        result = default!;
        error = null;
        mayStillBeRunning = false;

        var sync = new object();
        bool started = false;
        bool cancelledBeforeStart = false;
        var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);

        _mainThreadQueue.Enqueue(() =>
        {
            lock (sync)
            {
                if (cancelledBeforeStart)
                    return;
                started = true;
            }

            try { tcs.TrySetResult(func()); }
            catch (Exception ex) { tcs.TrySetException(ex); }
        });

        var completedTask = Task.WhenAny(tcs.Task, Task.Delay(timeoutMs)).GetAwaiter().GetResult();
        if (!ReferenceEquals(completedTask, tcs.Task))
        {
            lock (sync)
            {
                if (!started)
                    cancelledBeforeStart = true;
                mayStillBeRunning = started;
            }

            error = new TimeoutException(mayStillBeRunning
                ? $"Timed out after {timeoutMs} ms waiting for the game main-thread operation to finish."
                : $"Timed out after {timeoutMs} ms waiting for the game main thread to process the request.");
            return false;
        }

        if (tcs.Task.IsFaulted)
        {
            error = tcs.Task.Exception?.GetBaseException()
                ?? new InvalidOperationException("Main-thread operation failed.");
            return false;
        }

        if (tcs.Task.IsCanceled)
        {
            error = new TaskCanceledException(tcs.Task);
            return false;
        }

        result = tcs.Task.Result;
        return true;
    }

    private static bool TryReadOnMainThread<T>(
        HttpListenerResponse response,
        string operation,
        Func<T> func,
        out T result)
    {
        return TryRespondFromMainThread(
            response,
            operation,
            func,
            MainThreadReadTimeoutMs,
            isSideEffectingAction: false,
            out result);
    }

    private static bool TryActionOnMainThread<T>(
        HttpListenerResponse response,
        string operation,
        Func<T> func,
        out T result)
    {
        return TryRespondFromMainThread(
            response,
            operation,
            func,
            MainThreadActionTimeoutMs,
            isSideEffectingAction: true,
            out result);
    }

    private static bool TryRespondFromMainThread<T>(
        HttpListenerResponse response,
        string operation,
        Func<T> func,
        int timeoutMs,
        bool isSideEffectingAction,
        out T result)
    {
        if (TryRunOnMainThread(func, timeoutMs, out result, out var error, out bool mayStillBeRunning))
            return true;

        error ??= new InvalidOperationException("Main-thread operation failed.");
        GD.PrintErr($"[STS2 MCP] {operation}: {error}");

        bool isTimeout = error is TimeoutException;
        bool isEndpointModeError = error is EndpointModeException;
        response.StatusCode = isTimeout ? 503 : isEndpointModeError ? 409 : 500;

        var body = new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["error"] = isTimeout || isEndpointModeError
                ? error.Message
                : $"{operation} failed: {error.Message}",
            ["detail"] = error.Message,
            ["retryable"] = isTimeout && (!isSideEffectingAction || !mayStillBeRunning),
            ["may_have_applied"] = isSideEffectingAction && mayStillBeRunning ? true : null
        };

        if (!isTimeout && !isEndpointModeError)
        {
            body["exception_type"] = error.GetType().FullName;
            body["stack_trace"] = error.StackTrace;
        }

        SendJson(response, body);
        return false;
    }

    private static void ThrowIfWrongEndpointMode(bool expectedMultiplayer)
    {
        bool isMultiplayer = IsMultiplayerRun();
        if (isMultiplayer == expectedMultiplayer)
            return;

        throw new EndpointModeException(isMultiplayer
            ? "Multiplayer run is active. Use /api/v1/multiplayer instead."
            : "Not in a multiplayer run. Use /api/v1/singleplayer instead.");
    }

    private static bool TryReadRequestBody(
        HttpListenerRequest request,
        HttpListenerResponse response,
        out string body)
    {
        body = "";

        if (request.ContentLength64 > MaxRequestBodyChars)
        {
            SendError(response, 413, $"Request body is too large; maximum is {MaxRequestBodyChars} characters.");
            return false;
        }

        try
        {
            using var reader = new StreamReader(request.InputStream, request.ContentEncoding);
            using var timeout = new CancellationTokenSource(RequestBodyTimeoutMs);
            var readTask = reader.ReadToEndAsync(timeout.Token);
            var completedTask = Task.WhenAny(readTask, Task.Delay(RequestBodyTimeoutMs)).GetAwaiter().GetResult();

            if (!ReferenceEquals(completedTask, readTask))
            {
                timeout.Cancel();
                SendError(response, 408, "Timed out reading request body.");
                return false;
            }

            body = readTask.GetAwaiter().GetResult();
            if (body.Length > MaxRequestBodyChars)
            {
                SendError(response, 413, $"Request body is too large; maximum is {MaxRequestBodyChars} characters.");
                return false;
            }

            return true;
        }
        catch (Exception ex) when (ex is IOException or OperationCanceledException or ObjectDisposedException)
        {
            SendError(response, 408, $"Failed to read request body: {ex.Message}");
            return false;
        }
    }

    private static void ServerLoop()
    {
        while (_listener?.IsListening == true)
        {
            try
            {
                var context = _listener.GetContext();
                // Handle each request asynchronously so we don't block the listener
                ThreadPool.QueueUserWorkItem(_ => HandleRequest(context));
            }
            catch (HttpListenerException) { break; }
            catch (ObjectDisposedException) { break; }
            catch (Exception ex)
            {
                GD.PrintErr($"[STS2 MCP] Server loop error: {ex}");
                Thread.Sleep(100);
            }
        }
    }

    private static void HandleRequest(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            var response = context.Response;
            response.Headers.Add("Access-Control-Allow-Origin", "*");
            response.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            response.Headers.Add("Access-Control-Allow-Headers", "Content-Type");

            if (request.HttpMethod == "OPTIONS")
            {
                response.StatusCode = 204;
                response.Close();
                return;
            }

            string path = request.Url?.AbsolutePath ?? "/";

            if (path == "/")
            {
                SendJson(response, new { message = $"Hello from STS2 MCP v{Version}", status = "ok" });
            }
            else if (path == "/api/v1/singleplayer")
            {
                if (request.HttpMethod == "GET")
                    HandleGetState(request, response);
                else if (request.HttpMethod == "POST")
                    HandlePostAction(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/multiplayer")
            {
                if (request.HttpMethod == "GET")
                    HandleGetMultiplayerState(request, response);
                else if (request.HttpMethod == "POST")
                    HandlePostMultiplayerAction(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/profiles")
            {
                if (request.HttpMethod == "GET")
                    HandleGetProfiles(response);
                else if (request.HttpMethod == "POST")
                    HandlePostProfiles(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/profile")
            {
                if (request.HttpMethod == "GET")
                    HandleGetProfile(response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/compendium")
            {
                if (request.HttpMethod == "GET")
                    HandleGetCompendium(response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else if (path == "/api/v1/wiki")
            {
                if (request.HttpMethod == "GET")
                    HandleGetWiki(request, response);
                else
                    SendError(response, 405, "Method not allowed");
            }
            else
            {
                SendError(response, 404, "Not found");
            }
        }
        catch (Exception ex)
        {
            try
            {
                SendError(context.Response, 500, $"Internal error: {ex.Message}");
            }
            catch { /* response may already be closed */ }
        }
    }

    // Called on HTTP thread (not main thread) as a best-effort guard.
    // The try/catch handles race conditions during run transitions.
    // Authoritative checks happen inside RunOnMainThread lambdas.
    internal static bool IsMultiplayerRun()
    {
        try
        {
            return MegaCrit.Sts2.Core.Runs.RunManager.Instance.IsInProgress
                && MegaCrit.Sts2.Core.Runs.RunManager.Instance.NetService.Type.IsMultiplayer();
        }
        catch { return false; }
    }

    private static void HandleGetMultiplayerState(HttpListenerRequest request, HttpListenerResponse response)
    {
        string format = request.QueryString["format"] ?? "json";

        try
        {
            if (!TryReadOnMainThread(response, "Read multiplayer game state", () =>
                {
                    ThrowIfWrongEndpointMode(expectedMultiplayer: true);
                    return BuildMultiplayerGameState();
                }, out var state))
                return;

            if (format == "markdown")
            {
                string md = FormatAsMarkdown(state);
                SendText(response, md, "text/markdown");
            }
            else
            {
                SendJson(response, state);
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] HandleGetMultiplayerState: {ex}");
            try
            {
                response.StatusCode = 500;
                SendJson(response, new Dictionary<string, object?>
                {
                    ["error"] = $"Failed to read multiplayer game state: {ex.Message}",
                    ["exception_type"] = ex.GetType().FullName,
                    ["stack_trace"] = ex.StackTrace
                });
            }
            catch { /* response may be unusable */ }
        }
    }

    private static void HandlePostMultiplayerAction(HttpListenerRequest request, HttpListenerResponse response)
    {
        if (!TryReadRequestBody(request, response, out var body))
            return;

        Dictionary<string, JsonElement>? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(body);
        }
        catch
        {
            SendError(response, 400, "Invalid JSON");
            return;
        }

        if (parsed == null || !parsed.TryGetValue("action", out var actionElem))
        {
            SendError(response, 400, "Missing 'action' field");
            return;
        }

        string action = actionElem.GetString() ?? "";

        // Menu actions (FTUE/popup dismissal, game-over, character select, etc.) are
        // scene-tree-driven and equally valid in MP. Route them to the shared handler
        // so MP clients can dismiss blocking FTUE prompts without going through the
        // run-mode-specific dispatcher.
        if (action == "menu_select")
        {
            try
            {
                var option = parsed.TryGetValue("option", out var optElem) ? optElem.GetString() ?? "" : "";
                var seed = parsed.TryGetValue("seed", out var seedElem) ? seedElem.GetString() : null;
                if (!TryActionOnMainThread(response, "Execute multiplayer menu action", () =>
                    {
                        ThrowIfWrongEndpointMode(expectedMultiplayer: true);
                        return ExecuteMenuSelect(option, seed);
                    }, out var result))
                    return;
                SendJson(response, result);
            }
            catch (Exception ex)
            {
                SendError(response, 500, $"Menu action failed: {ex.Message}");
            }
            return;
        }

        try
        {
            if (!TryActionOnMainThread(response, "Execute multiplayer action", () =>
                {
                    ThrowIfWrongEndpointMode(expectedMultiplayer: true);
                    return ExecuteMultiplayerAction(action, parsed);
                }, out var result))
                return;
            SendJson(response, result);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Multiplayer action failed: {ex.Message}");
        }
    }

    private static void HandleGetState(HttpListenerRequest request, HttpListenerResponse response)
    {
        string format = request.QueryString["format"] ?? "json";

        try
        {
            if (!TryReadOnMainThread(response, "Read game state", () =>
                {
                    ThrowIfWrongEndpointMode(expectedMultiplayer: false);
                    return BuildGameState();
                }, out var state))
                return;

            if (format == "markdown")
            {
                try
                {
                    SendText(response, FormatAsMarkdown(state), "text/markdown");
                }
                catch (Exception ex)
                {
                    GD.PrintErr($"[STS2 MCP] FormatAsMarkdown failed, returning JSON: {ex}");
                    SendJson(response, state);
                }
            }
            else
            {
                SendJson(response, state);
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[STS2 MCP] HandleGetState: {ex}");
            try
            {
                response.StatusCode = 500;
                SendJson(response, new Dictionary<string, object?>
                {
                    ["error"] = $"Failed to read game state: {ex.Message}",
                    ["exception_type"] = ex.GetType().FullName,
                    ["stack_trace"] = ex.StackTrace
                });
            }
            catch { /* response may be unusable */ }
        }
    }

    private static void HandlePostAction(HttpListenerRequest request, HttpListenerResponse response)
    {
        if (!TryReadRequestBody(request, response, out var body))
            return;

        Dictionary<string, JsonElement>? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(body);
        }
        catch
        {
            SendError(response, 400, "Invalid JSON");
            return;
        }

        if (parsed == null || !parsed.TryGetValue("action", out var actionElem))
        {
            SendError(response, 400, "Missing 'action' field");
            return;
        }

        string action = actionElem.GetString() ?? "";

        // Handle menu actions separately (no run required)
        if (action == "menu_select")
        {
            try
            {
                var option = parsed.TryGetValue("option", out var optElem) ? optElem.GetString() ?? "" : "";
                var seed = parsed.TryGetValue("seed", out var seedElem) ? seedElem.GetString() : null;
                if (!TryActionOnMainThread(response, "Execute menu action", () =>
                    {
                        ThrowIfWrongEndpointMode(expectedMultiplayer: false);
                        return ExecuteMenuSelect(option, seed);
                    }, out var result))
                    return;
                SendJson(response, result);
            }
            catch (Exception ex)
            {
                SendError(response, 500, $"Menu action failed: {ex.Message}");
            }
            return;
        }

        try
        {
            if (!TryActionOnMainThread(response, "Execute action", () =>
                {
                    ThrowIfWrongEndpointMode(expectedMultiplayer: false);
                    return ExecuteAction(action, parsed);
                }, out var result))
                return;
            SendJson(response, result);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Action failed: {ex.Message}");
        }
    }
}
