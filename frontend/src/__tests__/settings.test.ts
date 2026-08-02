import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchSettings, regenerateInvite, regenerateCoach, renameGym } from "../api/settings";

describe("fetchSettings", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns SettingsData on a 200 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          gym_name: "Iron Temple",
          invite_code: "abc12345",
          invite_url: "https://t.me/testbot?start=abc12345",
          qr_svg: "<svg>...</svg>",
          coach_invite_code: "coach-xyz",
          coach_invite_url: "https://t.me/testbot?start=coach-xyz",
          bot_username: "testbot",
        }),
    } as Response);

    const data = await fetchSettings();
    expect(data.gym_name).toBe("Iron Temple");
    expect(data.invite_code).toBe("abc12345");
    expect(data.qr_svg).toBe("<svg>...</svg>");
    expect(data.coach_invite_code).toBe("coach-xyz");
  });

  it("throws on non-200 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 401,
    } as Response);

    await expect(fetchSettings()).rejects.toThrow("/api/settings: 401");
  });
});

describe("regenerateInvite", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sends confirm in the JSON body and returns new codes", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          invite_code: "new12345",
          invite_url: "https://t.me/testbot?start=new12345",
          qr_svg: "<svg>new...</svg>",
        }),
    } as Response);

    const data = await regenerateInvite("regenerar");
    expect(data.invite_code).toBe("new12345");
    expect(data.qr_svg).toBe("<svg>new...</svg>");
  });

  it("throws with error message from the body on 400", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ error: "wrong confirm" }),
    } as Response);

    await expect(regenerateInvite("no")).rejects.toThrow("wrong confirm");
  });
});

describe("regenerateCoach", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns new coach code and URL", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          coach_invite_code: "coach-new",
          coach_invite_url: "https://t.me/testbot?start=coach-new",
        }),
    } as Response);

    const data = await regenerateCoach("regenerar");
    expect(data.coach_invite_code).toBe("coach-new");
  });
});

describe("renameGym", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sends name in JSON and returns new name", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ gym_name: "New Gym" }),
    } as Response);

    const data = await renameGym("New Gym");
    expect(data.gym_name).toBe("New Gym");
  });

  it("throws with error message on 400", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: () =>
        Promise.resolve({ error: "The gym name cannot be empty." }),
    } as Response);

    await expect(renameGym("")).rejects.toThrow(
      "The gym name cannot be empty.",
    );
  });
});
