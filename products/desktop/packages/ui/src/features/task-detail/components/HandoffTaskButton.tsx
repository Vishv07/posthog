import { UserSwitch } from "@phosphor-icons/react";
import {
  DropdownMenu as QDropdownMenu,
  DropdownMenuContent as QDropdownMenuContent,
  DropdownMenuItem as QDropdownMenuItem,
  DropdownMenuTrigger as QDropdownMenuTrigger,
  Button as QuillButton,
} from "@posthog/quill";
import type { Task, UserBasic } from "@posthog/shared/domain-types";
import { useAuthStateValue } from "@posthog/ui/features/auth/store";
import { useCurrentUser } from "@posthog/ui/features/auth/useCurrentUser";
import { useOrgMembers } from "@posthog/ui/features/canvas/hooks/useOrgMembers";
import { userDisplayName } from "@posthog/ui/features/canvas/utils/userDisplay";
import { useHandoffTask } from "@posthog/ui/features/tasks/useTaskMutations";
import { toast } from "../../../primitives/toast";
import { logger } from "../../../shell/logger";

const log = logger.scope("task-detail");

/**
 * "Hand off" affordance in the task header: lets the task's owner pass
 * ownership to a colleague, who takes over driving it (the backend moves the
 * task, announces the handoff in the thread, and notifies the recipient).
 * Hidden unless the current user owns the task; everyone else just reads.
 */
export function HandoffTaskButton({ task }: { task: Task }) {
  const authStatus = useAuthStateValue((s) => s.status);
  const currentUser = useCurrentUser();
  const { mutate: handoffTask, isPending } = useHandoffTask();
  // Only fetched for the owner, the only case where the menu can open.
  const isOwner =
    authStatus === "authenticated" &&
    currentUser.data?.id != null &&
    task.created_by?.id === currentUser.data.id;
  const { members } = useOrgMembers({ enabled: isOwner });

  if (!isOwner) return null;

  const candidates = members.filter(
    (member) => member.id !== currentUser.data?.id,
  );
  if (candidates.length === 0) return null;

  const handleSelect = (member: UserBasic) => {
    const name = userDisplayName(member);
    handoffTask(
      { taskId: task.id, userId: member.id },
      {
        onSuccess: () => toast.success(`Handed "${task.title}" off to ${name}`),
        onError: (error) => {
          log.error("Failed to hand off task", error);
          toast.error("Couldn't hand off the task. Try again.");
        },
      },
    );
  };

  return (
    <div className="no-drag flex items-center">
      <QDropdownMenu>
        <QDropdownMenuTrigger
          render={
            <QuillButton variant="outline" size="sm" disabled={isPending} />
          }
        >
          <UserSwitch size={14} weight="regular" className="shrink-0" />
          Hand off
        </QDropdownMenuTrigger>
        <QDropdownMenuContent align="end">
          {candidates.map((member) => (
            <QDropdownMenuItem
              key={member.id}
              onClick={() => handleSelect(member)}
            >
              {userDisplayName(member)}
            </QDropdownMenuItem>
          ))}
        </QDropdownMenuContent>
      </QDropdownMenu>
    </div>
  );
}
