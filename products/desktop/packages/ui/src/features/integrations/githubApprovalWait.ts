import type { PostHogAPIClient } from "@posthog/api-client/posthog-client";
import { isGithubConnectPendingApproval } from "@posthog/core/integrations/connectErrors";
import { computeApprovedAfterPending } from "@posthog/core/integrations/connectMachine";
import { userGithubIntegrationKeys } from "@posthog/core/integrations/repositoryKeys";
import { ANALYTICS_EVENTS } from "@posthog/shared/analytics-events";
import { getAuthIdentity, useAuthStore } from "@posthog/ui/features/auth/store";
import {
  afterSettingsHydrated,
  useSettingsStore,
} from "@posthog/ui/features/settings/settingsStore";
import { toast } from "@posthog/ui/primitives/toast";
import { track } from "@posthog/ui/shell/analytics";
import type { QueryClient } from "@tanstack/react-query";

/** Lives at the shared connect choke point so every surface that connects
 * GitHub can close out an earlier "needs org owner approval" wait. Waits for
 * settings hydration because a cold-start deep link can drain before the
 * persisted store loads, and confirms an installation exists server-side
 * because the success deep link carries no nonce and can be forged. */
export function celebrateApprovalIfPending(
  queryClient: QueryClient,
  client: PostHogAPIClient | null,
): void {
  afterSettingsHydrated(() => void celebrateNow(queryClient, client));
}

async function celebrateNow(
  queryClient: QueryClient,
  client: PostHogAPIClient | null,
): Promise<void> {
  const {
    githubConnectPending,
    setGithubConnectPending,
    setLastUsedRunMode,
    setLastUsedWorkspaceMode,
  } = useSettingsStore.getState();
  const currentIdentity = getAuthIdentity(useAuthStore.getState().authState);
  const { shouldCelebrate, waitSeconds } = computeApprovedAfterPending({
    pending: githubConnectPending,
    currentIdentity,
    nowMs: Date.now(),
  });
  if (!shouldCelebrate || !client) return;
  try {
    const integrations = await queryClient.fetchQuery({
      queryKey: userGithubIntegrationKeys.list(),
      queryFn: () => client.getGithubUserIntegrations(),
    });
    if (integrations.length === 0) return;
  } catch {
    // Leave the marker so a real approval can still be celebrated once the
    // refetch succeeds.
    return;
  }
  track(ANALYTICS_EVENTS.ONBOARDING_GITHUB_CONNECT_APPROVED_AFTER_PENDING, {
    wait_seconds: waitSeconds,
  });
  setGithubConnectPending(null);
  // The next task's cloud-vs-local default is driven by lastUsedWorkspaceMode,
  // so set it to honor the toast; lastUsedRunMode is kept paired with it, the
  // way task creation does.
  setLastUsedWorkspaceMode("cloud");
  setLastUsedRunMode("cloud");
  toast.success(
    "GitHub is connected",
    "Your next tasks will run in the cloud.",
  );
}

/** Written at the shared choke point rather than in the onboarding panel:
 * the pending callback reaches whichever surface is mounted (on a cold start,
 * whichever drains it first), so a panel-local write could miss it. */
export function recordPendingApprovalWait(errorCode: string | null): void {
  if (!isGithubConnectPendingApproval(errorCode)) return;
  afterSettingsHydrated(() => {
    const currentIdentity = getAuthIdentity(useAuthStore.getState().authState);
    if (currentIdentity === null) return;
    const { githubConnectPending, setGithubConnectPending } =
      useSettingsStore.getState();
    if (githubConnectPending?.identity === currentIdentity) return;
    setGithubConnectPending({ identity: currentIdentity, since: Date.now() });
  });
}
