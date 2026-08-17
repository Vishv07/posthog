import { describe, expect, it } from "vitest";
import {
  CONNECT_INITIAL_STATUS,
  computeApprovedAfterPending,
  connectReducer,
  deriveConnectFlags,
  githubInvalidationKeys,
  slackInvalidationKeys,
  toConnectError,
} from "./connectMachine";

describe("connectReducer", () => {
  it("begin clears error and moves to connecting", () => {
    expect(
      connectReducer(
        { state: "error", error: { message: "x", code: null } },
        { type: "begin" },
      ),
    ).toEqual({ state: "connecting", error: null });
  });

  it("fail records the error", () => {
    const error = { message: "boom", code: "x" };
    expect(
      connectReducer(CONNECT_INITIAL_STATUS, { type: "fail", error }),
    ).toEqual({ state: "error", error });
  });

  it("succeed and reset return to idle", () => {
    expect(
      connectReducer(CONNECT_INITIAL_STATUS, { type: "succeed" }).state,
    ).toBe("idle");
    expect(
      connectReducer(CONNECT_INITIAL_STATUS, { type: "reset" }).state,
    ).toBe("idle");
  });

  it("timeout preserves the existing error", () => {
    const status = {
      state: "error" as const,
      error: { message: "e", code: null },
    };
    expect(connectReducer(status, { type: "timeout" })).toEqual({
      state: "timed-out",
      error: status.error,
    });
  });
});

describe("deriveConnectFlags", () => {
  it("derives boolean flags from state", () => {
    expect(deriveConnectFlags("connecting")).toEqual({
      isConnecting: true,
      isTimedOut: false,
      hasError: false,
    });
    expect(deriveConnectFlags("error").hasError).toBe(true);
    expect(deriveConnectFlags("timed-out").isTimedOut).toBe(true);
  });
});

describe("toConnectError", () => {
  it("uses the error message when given an Error", () => {
    expect(toConnectError(new Error("nope"), "fallback")).toEqual({
      message: "nope",
      code: null,
    });
  });

  it("falls back for non-Error values", () => {
    expect(toConnectError("x", "fallback").message).toBe("fallback");
  });
});

describe("invalidation keys", () => {
  it("omits the project key when projectId is null", () => {
    expect(githubInvalidationKeys(null)).toEqual([
      ["integrations", "list"],
      ["user-github-integrations"],
      ["github_login"],
    ]);
  });

  it("includes the project key when projectId is set", () => {
    expect(githubInvalidationKeys(7)[0]).toEqual(["integrations", 7]);
  });

  it("slack keys cover list and root", () => {
    expect(slackInvalidationKeys()).toEqual([
      ["integrations", "list"],
      ["integrations"],
    ]);
  });
});

describe("computeApprovedAfterPending", () => {
  it("does not celebrate when nothing was pending", () => {
    expect(
      computeApprovedAfterPending({
        pending: null,
        currentIdentity: "us:1",
        nowMs: Date.now(),
      }),
    ).toEqual({
      shouldCelebrate: false,
      waitSeconds: 0,
    });
  });

  it("celebrates and reports the elapsed wait when the pending identity matches", () => {
    const since = 1_000;
    const now = since + 90_000;
    expect(
      computeApprovedAfterPending({
        pending: { identity: "us:1", since },
        currentIdentity: "us:1",
        nowMs: now,
      }),
    ).toEqual({
      shouldCelebrate: true,
      waitSeconds: 90,
    });
  });

  it("does not celebrate when the pending marker belongs to a different account", () => {
    expect(
      computeApprovedAfterPending({
        pending: { identity: "us:1", since: 1_000 },
        currentIdentity: "us:2",
        nowMs: 91_000,
      }),
    ).toEqual({
      shouldCelebrate: false,
      waitSeconds: 0,
    });
  });

  it("never reports a negative wait if the clock moved backwards", () => {
    expect(
      computeApprovedAfterPending({
        pending: { identity: "us:1", since: 10_000 },
        currentIdentity: "us:1",
        nowMs: 1_000,
      }),
    ).toEqual({
      shouldCelebrate: true,
      waitSeconds: 0,
    });
  });
});
