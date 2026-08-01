import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchSession, SessionAuthError } from "../api/session";

describe("fetchSession", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns SessionData on a 200 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ name: "Ana", gym: "Iron Temple" }),
    } as Response);

    const data = await fetchSession();
    expect(data).toEqual({ name: "Ana", gym: "Iron Temple" });
  });

  it("throws SessionAuthError on a 401 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 401,
    } as Response);

    await expect(fetchSession()).rejects.toBeInstanceOf(SessionAuthError);
  });

  it("throws a generic Error (not SessionAuthError) on a 500 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    await expect(fetchSession()).rejects.toThrow("/api/session: 500");
    await expect(fetchSession()).rejects.not.toBeInstanceOf(SessionAuthError);
  });
});
