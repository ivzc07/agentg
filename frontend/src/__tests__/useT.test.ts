import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useT } from "../hooks/useT";

describe("useT", () => {
  beforeEach(() => {
    delete (window as any).__I18N__;
  });

  it("returns the value for a known key", () => {
    window.__I18N__ = { settings: "Settings", member_eyebrow: "member" };
    const { result } = renderHook(() => useT());
    const t = result.current;
    expect(t("settings")).toBe("Settings");
  });

  it("returns the key itself for a missing key", () => {
    window.__I18N__ = { settings: "Settings" };
    const { result } = renderHook(() => useT());
    const t = result.current;
    expect(t("nonexistent_key")).toBe("nonexistent_key");
  });

  it("returns the key when window.__I18N__ is absent", () => {
    const { result } = renderHook(() => useT());
    const t = result.current;
    expect(t("settings")).toBe("settings");
  });
});
