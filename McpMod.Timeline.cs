using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Saves;

namespace STS2_MCP;

public static partial class McpMod
{
    private static void HandleGetTimeline(HttpListenerResponse response)
    {
        try
        {
            if (!TryReadOnMainThread(response, "Build timeline status", BuildTimelineStatus, out var status))
                return;
            SendJson(response, status);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Failed to build timeline status: {ex.Message}");
        }
    }

    private static void HandlePostTimeline(HttpListenerRequest request, HttpListenerResponse response)
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
            SendError(response, 400, "Missing 'action' field. Use: reveal_pending");
            return;
        }

        var action = actionElem.GetString() ?? "";
        var dryRun = parsed.TryGetValue("dry_run", out var dryRunElem)
            && dryRunElem.ValueKind == JsonValueKind.True;

        try
        {
            if (!TryActionOnMainThread(response, "Execute timeline action", () => ExecuteTimelineAction(action, dryRun), out var result))
                return;
            SendJson(response, result);
        }
        catch (Exception ex)
        {
            SendError(response, 500, $"Timeline action failed: {ex.Message}");
        }
    }

    private static Dictionary<string, object?> ExecuteTimelineAction(string action, bool dryRun)
    {
        var normalizedAction = action.Trim().ToLowerInvariant();
        return normalizedAction switch
        {
            "status" => BuildTimelineStatus(),
            "reveal" or "reveal_pending" or "reveal-pending" => RevealPendingTimelineEpochs(dryRun),
            _ => Error($"Unknown timeline action: {action}. Use: status, reveal_pending")
        };
    }

    private static Dictionary<string, object?> BuildTimelineStatus()
    {
        var sm = SaveManager.Instance;
        var progress = sm?.Progress;
        if (sm == null || progress == null)
            return Error("Save manager progress is not available");

        var epochs = progress.Epochs.Select(BuildTimelineEpochSummary).ToList();
        var pending = epochs
            .Where(epoch => IsObtainedUnrevealedState(epoch.TryGetValue("state", out var state) ? state?.ToString() : null))
            .Select(epoch => epoch["id"]?.ToString())
            .Where(id => !string.IsNullOrWhiteSpace(id))
            .Cast<string>()
            .ToList();
        var pendingSlotUnlocks = epochs
            .Where(epoch => IsObtainedNoSlotState(epoch.TryGetValue("state", out var state) ? state?.ToString() : null))
            .Select(epoch => epoch["id"]?.ToString())
            .Where(id => !string.IsNullOrWhiteSpace(id))
            .Cast<string>()
            .ToList();
        var blockedPending = GetMainMenuBlockedTimelinePendingEpochIds();

        return new Dictionary<string, object?>
        {
            ["status"] = "ok",
            ["current_profile_id"] = sm.CurrentProfileId,
            ["progress_path"] = ResolveProfileProgressPath(sm.CurrentProfileId),
            ["pending_epoch_ids"] = pending,
            ["pending_slot_unlock_epoch_ids"] = pendingSlotUnlocks,
            ["pending_count"] = pending.Count,
            ["main_menu_blocked_pending_epoch_ids"] = blockedPending,
            ["is_main_menu_blocked"] = blockedPending != null,
            ["epochs"] = epochs
        };
    }

    private static Dictionary<string, object?> RevealPendingTimelineEpochs(bool dryRun)
    {
        var sm = SaveManager.Instance;
        var progress = sm?.Progress;
        if (sm == null || progress == null)
            return Error("Save manager progress is not available");

        var pendingEpochsFromProgress = progress.Epochs
            .Where(epoch => IsObtainedUnrevealedState(epoch.State.ToString()))
            .ToList();
        var pendingIds = pendingEpochsFromProgress.Select(epoch => epoch.Id).ToList();
        var pendingSlotUnlockIds = pendingEpochsFromProgress
            .Where(epoch => IsObtainedNoSlotState(epoch.State.ToString()))
            .Select(epoch => epoch.Id)
            .ToList();

        if (dryRun || pendingEpochsFromProgress.Count == 0)
        {
            return new Dictionary<string, object?>
            {
                ["status"] = "ok",
                ["dry_run"] = dryRun,
                ["current_profile_id"] = sm.CurrentProfileId,
                ["progress_path"] = ResolveProfileProgressPath(sm.CurrentProfileId),
                ["pending_epoch_ids"] = pendingIds,
                ["pending_slot_unlock_epoch_ids"] = pendingSlotUnlockIds,
                ["revealed_epoch_ids"] = new List<string>(),
                ["pending_count"] = pendingEpochsFromProgress.Count,
                ["changed"] = false
            };
        }

        var blockedPendingIds = GetMainMenuBlockedTimelinePendingEpochIds();
        if (blockedPendingIds == null || blockedPendingIds.Count == 0)
        {
            return Error(
                "Timeline reveal is only allowed from the main-menu Timeline blocker. " +
                "Return to the blocked main menu or use dry_run to inspect pending epochs."
            );
        }

        var blockedPendingSet = new HashSet<string>(blockedPendingIds, StringComparer.OrdinalIgnoreCase);
        var pendingEpochs = pendingEpochsFromProgress
            .Where(epoch => blockedPendingSet.Contains(epoch.Id))
            .ToList();
        if (pendingEpochs.Count != blockedPendingSet.Count)
        {
            return new Dictionary<string, object?>
            {
                ["status"] = "error",
                ["error"] = "Main-menu Timeline blocker references epochs that are not pending in profile progress",
                ["blocked_pending_epoch_ids"] = blockedPendingIds,
                ["pending_epoch_ids"] = pendingIds
            };
        }

        var blockedSlotUnlockIds = pendingEpochs
            .Where(epoch => IsObtainedNoSlotState(epoch.State.ToString()))
            .Select(epoch => epoch.Id)
            .ToList();
        if (blockedSlotUnlockIds.Count > 0)
        {
            return new Dictionary<string, object?>
            {
                ["status"] = "error",
                ["error"] = "Timeline blocker includes epochs in ObtainedNoSlot state; refusing to mark them Revealed because they require slot/unlock side effects before reveal",
                ["pending_epoch_ids"] = pendingIds,
                ["pending_slot_unlock_epoch_ids"] = blockedSlotUnlockIds,
                ["manual_action_required"] = true
            };
        }

        var originalStates = pendingEpochs
            .Select(epoch => new EpochStateSnapshot(epoch, GetObjectValue(epoch, "State")))
            .ToList();
        var revealedIds = new List<string>();
        var failures = new List<Dictionary<string, object?>>();
        foreach (var epoch in pendingEpochs)
        {
            if (TryRevealEpoch(progress, epoch, out var failure))
            {
                revealedIds.Add(epoch.Id);
            }
            else
            {
                failures.Add(new Dictionary<string, object?>
                {
                    ["id"] = epoch.Id,
                    ["error"] = failure
                });
            }
        }

        if (failures.Count > 0)
        {
            RollBackEpochStates(originalStates);
            return new Dictionary<string, object?>
            {
                ["status"] = "error",
                ["error"] = "Failed to reveal one or more pending Timeline epochs",
                ["failures"] = failures,
                ["pending_epoch_ids"] = pendingIds,
                ["revealed_epoch_ids"] = revealedIds,
                ["rolled_back"] = true
            };
        }

        var saveResult = TrySaveProgressAfterTimelineReveal(out var saveDetail);
        if (!saveResult)
        {
            RollBackEpochStates(originalStates);
            return new Dictionary<string, object?>
            {
                ["status"] = "error",
                ["error"] = "Revealed pending Timeline epochs in memory but could not persist progress; in-memory states were rolled back",
                ["current_profile_id"] = sm.CurrentProfileId,
                ["progress_path"] = ResolveProfileProgressPath(sm.CurrentProfileId),
                ["previously_pending_epoch_ids"] = pendingIds,
                ["pending_epoch_ids"] = pendingIds,
                ["remaining_pending_epoch_ids"] = pendingIds,
                ["revealed_epoch_ids"] = new List<string>(),
                ["revealed_count"] = 0,
                ["changed"] = false,
                ["rolled_back"] = true,
                ["save_result"] = saveDetail
            };
        }

        var remainingPendingIds = progress.Epochs
            .Where(epoch => IsObtainedUnrevealedState(epoch.State.ToString()))
            .Select(epoch => epoch.Id)
            .ToList();

        return new Dictionary<string, object?>
        {
            ["status"] = "ok",
            ["current_profile_id"] = sm.CurrentProfileId,
            ["progress_path"] = ResolveProfileProgressPath(sm.CurrentProfileId),
            ["previously_pending_epoch_ids"] = pendingIds,
            ["pending_epoch_ids"] = remainingPendingIds,
            ["remaining_pending_epoch_ids"] = remainingPendingIds,
            ["pending_slot_unlock_epoch_ids"] = remainingPendingIds
                .Where(id => progress.Epochs.Any(epoch =>
                    string.Equals(epoch.Id, id, StringComparison.OrdinalIgnoreCase)
                    && IsObtainedNoSlotState(epoch.State.ToString())))
                .ToList(),
            ["revealed_epoch_ids"] = revealedIds,
            ["revealed_count"] = revealedIds.Count,
            ["changed"] = revealedIds.Count > 0,
            ["save_result"] = saveDetail
        };
    }

    private sealed record EpochStateSnapshot(object Epoch, object? State);

    private static Dictionary<string, object?> BuildTimelineEpochSummary(object epoch)
    {
        return new Dictionary<string, object?>
        {
            ["id"] = GetObjectValue(epoch, "Id")?.ToString(),
            ["state"] = GetObjectValue(epoch, "State")?.ToString(),
            ["obtained"] = GetObjectValue(epoch, "ObtainDate")
        };
    }

    private static bool IsObtainedUnrevealedState(string? state)
    {
        return string.Equals(state, "Obtained", StringComparison.OrdinalIgnoreCase)
            || string.Equals(state, "obtained", StringComparison.OrdinalIgnoreCase)
            || IsObtainedNoSlotState(state);
    }

    private static bool IsObtainedNoSlotState(string? state)
    {
        return string.Equals(state, "ObtainedNoSlot", StringComparison.OrdinalIgnoreCase)
            || string.Equals(state, "obtained_no_slot", StringComparison.OrdinalIgnoreCase);
    }

    private static bool TryRevealEpoch(object progress, object epoch, out string? failure)
    {
        failure = null;
        if (!TryInvokeProgressRevealEpoch(progress, epoch, out failure))
            return false;

        if (string.Equals(GetObjectValue(epoch, "State")?.ToString(), "Revealed", StringComparison.OrdinalIgnoreCase))
            return true;

        failure ??= "Progress RevealEpoch did not set epoch state to Revealed";
        return false;
    }

    private static bool TryInvokeProgressRevealEpoch(object progress, object epoch, out string? failure)
    {
        failure = null;
        var epochId = GetObjectValue(epoch, "Id")?.ToString();
        var methods = progress.GetType()
            .GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .Where(method => method.Name == "RevealEpoch" && method.GetParameters().Length == 1);

        foreach (var method in methods)
        {
            var parameterType = method.GetParameters()[0].ParameterType;
            if (!TryBuildRevealEpochArgument(parameterType, epoch, epochId, out var argument))
                continue;

            try
            {
                method.Invoke(progress, new[] { argument });
                return true;
            }
            catch (TargetInvocationException ex)
            {
                failure = ex.InnerException?.Message ?? ex.Message;
            }
            catch (Exception ex)
            {
                failure = ex.Message;
            }
        }

        failure ??= $"No compatible Progress RevealEpoch overload found for epoch {epochId ?? "<unknown>"}";
        return false;
    }

    private static bool TryBuildRevealEpochArgument(Type parameterType, object epoch, string? epochId, out object? argument)
    {
        argument = null;
        if (parameterType.IsAssignableFrom(epoch.GetType()))
        {
            argument = epoch;
            return true;
        }

        if (parameterType == typeof(string) && !string.IsNullOrWhiteSpace(epochId))
        {
            argument = epochId;
            return true;
        }

        if (!string.IsNullOrWhiteSpace(epochId))
        {
            var stringConstructor = parameterType.GetConstructor(new[] { typeof(string) });
            if (stringConstructor != null)
            {
                argument = stringConstructor.Invoke(new object[] { epochId });
                return true;
            }
        }

        return false;
    }

    private static void RollBackEpochStates(IEnumerable<EpochStateSnapshot> snapshots)
    {
        foreach (var snapshot in snapshots)
        {
            if (snapshot.State == null)
                continue;
            SetEpochStateValue(snapshot.Epoch, snapshot.State);
        }
    }

    private static bool SetEpochStateValue(object epoch, object state)
    {
        var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        var property = epoch.GetType().GetProperty("State", flags);
        if (property?.SetMethod != null)
        {
            property.SetValue(epoch, state);
            return true;
        }

        var backingField = epoch.GetType().GetField("<State>k__BackingField", flags)
            ?? epoch.GetType().GetField("_state", flags)
            ?? epoch.GetType().GetField("state", flags);
        if (backingField != null)
        {
            backingField.SetValue(epoch, state);
            return true;
        }

        return false;
    }

    private static List<string>? GetMainMenuBlockedTimelinePendingEpochIds()
    {
        try
        {
            var state = BuildGameState();
            if (!string.Equals(state.TryGetValue("state_type", out var stateType) ? stateType?.ToString() : null, "menu", StringComparison.OrdinalIgnoreCase))
                return null;
            if (!string.Equals(state.TryGetValue("menu_screen", out var screen) ? screen?.ToString() : null, "main", StringComparison.OrdinalIgnoreCase))
                return null;
            if (!state.TryGetValue("blocked_options", out var blockedOptionsObj)
                || blockedOptionsObj is not IEnumerable<Dictionary<string, object?>> blockedOptions)
            {
                return null;
            }

            foreach (var option in blockedOptions)
            {
                var name = option.TryGetValue("name", out var nameObj) ? nameObj?.ToString() : null;
                var reason = option.TryGetValue("reason", out var reasonObj) ? reasonObj?.ToString() : null;
                if (!string.Equals(name, "timeline", StringComparison.OrdinalIgnoreCase)
                    || !string.Equals(reason, "manual_epoch_reveal_required", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                if (!option.TryGetValue("pending_epoch_ids", out var idsObj) || idsObj == null)
                    return new List<string>();

                if (idsObj is IEnumerable<string> stringIds)
                    return stringIds.ToList();

                if (idsObj is System.Collections.IEnumerable enumerable && idsObj is not string)
                {
                    var ids = new List<string>();
                    foreach (var item in enumerable)
                    {
                        var id = item?.ToString();
                        if (!string.IsNullOrWhiteSpace(id))
                            ids.Add(id);
                    }
                    return ids;
                }

                return new List<string> { idsObj.ToString() ?? "" }
                    .Where(id => !string.IsNullOrWhiteSpace(id))
                    .ToList();
            }
        }
        catch
        {
            return null;
        }

        return null;
    }

    private static bool TrySaveProgressAfterTimelineReveal(out Dictionary<string, object?> detail)
    {
        var attempts = new List<string>();
        var sm = SaveManager.Instance;
        if (sm == null)
        {
            detail = new Dictionary<string, object?> { ["attempts"] = attempts, ["error"] = "Save manager is not available" };
            return false;
        }

        if (TryInvokeZeroArgSaveMethod(sm, new[] { "SaveProgress" }, attempts, out detail))
            return true;

        var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        var progressSaveManager = sm.GetType().GetField("_progressSaveManager", flags)?.GetValue(sm)
            ?? sm.GetType().GetProperty("ProgressSaveManager", flags)?.GetValue(sm);
        if (progressSaveManager != null
            && TryInvokeZeroArgSaveMethod(progressSaveManager, new[] { "SaveProgress", "SaveProgressFile", "Save" }, attempts, out detail))
        {
            return true;
        }

        detail = new Dictionary<string, object?>
        {
            ["attempts"] = attempts,
            ["error"] = "No usable zero-argument progress save method was found"
        };
        return false;
    }

    private static bool TryInvokeZeroArgSaveMethod(
        object target,
        IEnumerable<string> names,
        List<string> attempts,
        out Dictionary<string, object?> detail)
    {
        var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        foreach (var name in names)
        {
            var method = target.GetType()
                .GetMethods(flags)
                .FirstOrDefault(candidate => candidate.Name == name && candidate.GetParameters().Length == 0);
            if (method == null)
                continue;

            attempts.Add($"{target.GetType().Name}.{method.Name}()");
            try
            {
                var result = method.Invoke(target, null);
                if (result is Task task)
                    task.GetAwaiter().GetResult();
                detail = new Dictionary<string, object?>
                {
                    ["method"] = $"{target.GetType().FullName}.{method.Name}",
                    ["attempts"] = attempts
                };
                return true;
            }
            catch (TargetInvocationException ex)
            {
                detail = new Dictionary<string, object?>
                {
                    ["method"] = $"{target.GetType().FullName}.{method.Name}",
                    ["attempts"] = attempts,
                    ["error"] = ex.InnerException?.Message ?? ex.Message
                };
                return false;
            }
            catch (Exception ex)
            {
                detail = new Dictionary<string, object?>
                {
                    ["method"] = $"{target.GetType().FullName}.{method.Name}",
                    ["attempts"] = attempts,
                    ["error"] = ex.Message
                };
                return false;
            }
        }

        detail = new Dictionary<string, object?> { ["attempts"] = attempts };
        return false;
    }

    private static object? GetObjectValue(object owner, string propertyName)
    {
        var flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        return owner.GetType().GetProperty(propertyName, flags)?.GetValue(owner)
            ?? owner.GetType().GetField(propertyName, flags)?.GetValue(owner)
            ?? owner.GetType().GetField($"<{propertyName}>k__BackingField", flags)?.GetValue(owner);
    }
}
