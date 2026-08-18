import type { Task } from "@posthog/shared/domain-types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const currentUserId = vi.hoisted(() => ({ id: 1 as number | undefined }));
const mockHandoffMutate = vi.hoisted(() => vi.fn());
const mockMembers = vi.hoisted(() => ({
  members: [
    { id: 1, uuid: "u-1", email: "owner@example.com", first_name: "Owner" },
    { id: 2, uuid: "u-2", email: "colleague@example.com", first_name: "Col" },
  ],
}));

vi.mock("@posthog/ui/features/auth/store", () => ({
  useAuthStateValue: (selector: (state: { status: string }) => unknown) =>
    selector({ status: "authenticated" }),
}));
vi.mock("@posthog/ui/features/auth/useCurrentUser", () => ({
  useCurrentUser: () => ({ data: { id: currentUserId.id } }),
}));
vi.mock("@posthog/ui/features/canvas/hooks/useOrgMembers", () => ({
  useOrgMembers: () => ({ members: mockMembers.members }),
}));
vi.mock("@posthog/ui/features/tasks/useTaskMutations", () => ({
  useHandoffTask: () => ({ mutate: mockHandoffMutate, isPending: false }),
}));

import { HandoffTaskButton } from "./HandoffTaskButton";

function createTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    task_number: 1,
    slug: "task-1",
    title: "Fix the thing",
    description: "",
    created_at: "2026-05-28T00:00:00.000Z",
    updated_at: "2026-05-28T00:00:00.000Z",
    origin_product: "user_created",
    created_by: { id: 1, uuid: "u-1", email: "owner@example.com" },
    ...overrides,
  };
}

function renderButton(task: Task) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<HandoffTaskButton task={task} />, { wrapper });
}

describe("HandoffTaskButton", () => {
  it("renders nothing when the current user does not own the task", () => {
    // Otherwise a teammate would get offered a handoff that only ever 404s.
    currentUserId.id = 3;
    const { container } = renderButton(createTask());
    expect(container).toBeEmptyDOMElement();
  });

  it("hands off to the selected member with their user id", async () => {
    currentUserId.id = 1;
    mockHandoffMutate.mockClear();
    const user = userEvent.setup();
    renderButton(createTask());

    await user.click(screen.getByRole("button", { name: /hand off/i }));
    await user.click(await screen.findByText("Col"));

    expect(mockHandoffMutate).toHaveBeenCalledWith(
      { taskId: "task-1", userId: 2 },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("does not offer the current owner as a handoff target", async () => {
    currentUserId.id = 1;
    const user = userEvent.setup();
    renderButton(createTask());

    await user.click(screen.getByRole("button", { name: /hand off/i }));

    expect(screen.queryByText("Owner")).not.toBeInTheDocument();
    expect(await screen.findByText("Col")).toBeInTheDocument();
  });
});
