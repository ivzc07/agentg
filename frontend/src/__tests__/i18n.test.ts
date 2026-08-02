import { describe, it, expect, beforeEach } from "vitest";
import { getI18N } from "../lib/i18n";

describe("getI18N", () => {
  beforeEach(() => {
    delete (window as any).__I18N__;
  });

  it("returns window.__I18N__ when it is set", () => {
    window.__I18N__ = { hello: "world" };
    expect(getI18N()).toEqual({ hello: "world" });
  });

  it("returns an empty object when window.__I18N__ is absent", () => {
    expect(getI18N()).toEqual({});
  });
});
