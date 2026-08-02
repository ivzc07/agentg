import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchMember, tickOffFlag, MemberNotFoundError, MemberAuthError } from "../api/member";

describe("fetchMember", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns MemberPageData on a 200 response", async () => {
    const payload = { member_id: 1, name: "Ana" };
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
    } as Response);

    const data = await fetchMember(1);
    expect(data).toEqual(payload);
  });

  it("appends page query string when page > 1", async () => {
    const payload = { member_id: 1, page: 2 };
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve(payload),
    } as Response);

    await fetchMember(1, 2);
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/members/1?page=2");
  });

  it("omits page query string when page is 1", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    } as Response);

    await fetchMember(1, 1);
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/members/1");
  });

  it("throws MemberAuthError on a 401 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 401,
    } as Response);

    const err = await fetchMember(1).catch((e) => e);
    expect(err).toBeInstanceOf(MemberAuthError);
    expect(err.message).toBe("/api/members/1: 401");
  });

  it("throws MemberNotFoundError on a 404 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 404,
    } as Response);

    const err = await fetchMember(99).catch((e) => e);
    expect(err).toBeInstanceOf(MemberNotFoundError);
    expect(err.message).toBe("/api/members/99: not found");
  });

  it("throws a generic Error on a 500 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    await expect(fetchMember(1)).rejects.toThrow("/api/members/1: 500");
  });
});

describe("tickOffFlag", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("POSTs to the tick-off URL and returns the body", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ note_id: 42, acknowledged: true }),
    } as Response);

    const result = await tickOffFlag(1, 42);
    expect(result).toEqual({ note_id: 42, acknowledged: true });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/members/1/flags/42/tick-off",
      { method: "POST" }
    );
  });

  it("throws MemberAuthError on a 401 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 401,
    } as Response);

    const err = await tickOffFlag(1, 42).catch((e) => e);
    expect(err).toBeInstanceOf(MemberAuthError);
    expect(err.message).toBe("/api/members/1/flags/42/tick-off: 401");
  });

  it("throws a generic Error on a 500 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    await expect(tickOffFlag(1, 42)).rejects.toThrow("/api/members/1/flags/42/tick-off: 500");
  });
});
