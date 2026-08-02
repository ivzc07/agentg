import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoutineEditor } from "../components/RoutineEditor";
// Vite serves the component's own source as a string, so the guard below reads
// production code without pulling node builtins into the typechecked build.
import routineEditorSource from "../components/RoutineEditor.tsx?raw";
import tailwindConfig from "../../tailwind.config";

// ---------------------------------------------------------------------------
//  Text-colour utility inventory — derived from the live Tailwind config so
//  the tests stay in sync when design tokens are added or removed.  The
//  inventory covers project custom colours, standard Tailwind keywords, and
//  the full default palette.  Arbitrary-value classes (text-[14px]) are never
//  colour utilities.
// ---------------------------------------------------------------------------

/** Build a Set of known `text-*` colour-utility class names. */
function buildTextColorInventory(): Set<string> {
  const set = new Set<string>();

  // Standard fixed Tailwind colour keywords
  for (const kw of ["white", "black", "current", "transparent", "inherit"]) {
    set.add(`text-${kw}`);
  }

  // Project design tokens from tailwind.config.ts theme.extend.colors
  const colors = (tailwindConfig.theme?.extend?.colors ?? {}) as Record<
    string,
    unknown
  >;
  for (const [key, val] of Object.entries(colors)) {
    if (typeof val === "string") {
      set.add(`text-${key}`);
    } else if (typeof val === "object" && val !== null) {
      // Nested colour object: the key itself is the DEFAULT shade.
      set.add(`text-${key}`);
      for (const sub of Object.keys(val as Record<string, unknown>)) {
        // DEFAULT is an alias for the key — skip the explicit form.
        if (sub !== "DEFAULT") set.add(`text-${key}-${sub}`);
      }
    }
  }

  // Standard Tailwind palette: text-{name}-{shade}
  const PALETTE_NAMES = [
    "slate",
    "gray",
    "zinc",
    "neutral",
    "stone",
    "red",
    "orange",
    "amber",
    "yellow",
    "lime",
    "green",
    "emerald",
    "teal",
    "cyan",
    "sky",
    "blue",
    "indigo",
    "violet",
    "purple",
    "fuchsia",
    "pink",
    "rose",
  ];
  const SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];
  for (const name of PALETTE_NAMES) {
    for (const shade of SHADES) {
      set.add(`text-${name}-${shade}`);
    }
  }

  return set;
}

const TEXT_COLOR_INVENTORY = buildTextColorInventory();

/** True when `value` looks like a colour: hex, CSS colour function, or a
 *  CSS variable that resolves to a colour. */
function looksLikeColor(value: string): boolean {
  // Hex colour: #rgb, #rrggbb, #rgba, #rrggbbaa
  if (/^#[0-9a-fA-F]{3,8}$/.test(value)) return true;
  // Colour functions
  if (/^(rgb|rgba|hsl|hsla|hwb|lab|lch|oklch|oklab|color)\(/.test(value)) return true;
  // CSS variable — likely a colour variable (e.g. var(--foreground))
  if (/^var\(--/.test(value)) return true;
  return false;
}

/** True when the content inside `text-[...]` looks like a colour (hex, rgb(),
 *  hsl(), etc.).  Also recognises typed arbitrary values such as
 *  `text-[color:#fff]` and `text-[color:var(--foreground)]`.  Returns false
 *  for sizing values like `text-[14px]`, non-colour typed values like
 *  `text-[font-size:14px]`, and ambiguous bare CSS variables like
 *  `text-[--custom]`. */
function isArbitraryTextColor(cls: string): boolean {
  // Match text-[<value>] or text-[<type>:<value>] with an optional
  // /<alpha-modifier>.  Modifiers may be numeric (/50) or arbitrary
  // bracket opacity (/[.5], /[var(--my-opacity)]).
  const m = cls.match(/^text-\[(.+?)\](?:\/(?:\d+|\[[^\]]*\]))?$/);
  if (!m) return false;
  const inner = m[1];

  // Typed arbitrary value: text-[color:#fff], text-[color:var(--foreground)], etc.
  // Only match when the type is "color"; reject text-[font-size:14px], etc.
  const typed = inner.match(/^color:(.+)$/);
  if (typed) return looksLikeColor(typed[1]);

  // Bare arbitrary value: text-[#fff], text-[rgb(…)], etc.
  return looksLikeColor(inner);
}

/** Strip a Tailwind slash-opacity modifier (e.g. text-bg/50 → text-bg,
 *  text-red-500/25 → text-red-500) so the base utility can be classified
 *  against the inventory.  Non-slash classes are returned unchanged. */
function stripOpacityModifier(cls: string): string {
  // Strip arbitrary bracket opacity (/[.5], /[0.5]) first, then
  // numeric opacity (/50, /25).  Both are valid Tailwind modifiers.
  return cls
    .replace(/\/\[[^\]]*\]$/, "")
    .replace(/\/\d+$/, "");
}

/** True when `cls` is a `text-*` colour utility.  Also detects arbitrary-value
 *  colour classes like `text-[#fff]`.  Returns false for sizing, alignment,
 *  decoration, and other non-colour `text-*` utilities.
 *
 *  Slash-opacity variants (text-bg/50, text-white/50, text-red-500/25) are
 *  classified by stripping the modifier and checking the base class. */
function isTextColorClass(cls: string): boolean {
  if (!cls.startsWith("text-")) return false;
  // Strip slash-opacity modifier before classifying (P2, PR review)
  const base = stripOpacityModifier(cls);
  // Known tokens from the inventory (arbitrary-value classes never match here)
  if (TEXT_COLOR_INVENTORY.has(base)) return true;
  // Arbitrary-value colour classes: text-[#fff], text-[rgb(…)], etc.
  // (isArbitraryTextColor already handles its own slash-opacity suffix)
  if (isArbitraryTextColor(base)) return true;
  return false;
}

/** Split a className string into individual tokens. */
function classTokens(className: string): string[] {
  return className.split(/\s+/).filter(Boolean);
}

const EN_BOOTSTRAP = {
  editor_title: "{name}'s routine",
  chip_agent: "Agent-managed",
  chip_coach: "Coach-authored",
  chip_coach_named: "Coach-authored \u2014 {name}",
  preset_chip: "Preset: {name}",
  chip_consequence:
    "Saving makes this plan yours — the Agent will stop adjusting it.",
  routine_saved: "Routine saved.",
  member_notified: "We told {name}.",
  stale_error: "This routine changed while you were editing.",
  current_version_label: "Current version",
  pick_day: "\u2014 day \u2014",
  workout_name_placeholder: "Name (e.g. Legs)",
  catalog_label: "Exercise catalog",
  editor_help: "One exercise per line: name, sets, reps.",
  save_routine: "Save Routine",
  network_error: "Network error — please try again.",
  member_not_found: "Member not found.",
  remove_day: "Remove day",
  remove_exercise: "Remove exercise",
  add_day: "Add day",
  add_exercise: "Add exercise",
  _weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
};

beforeEach(() => {
  (window as any).__I18N__ = { ...EN_BOOTSTRAP };
});

function renderEditor(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route
              path="/members/:memberId/routine"
              element={<RoutineEditor />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    ),
    queryClient,
  };
}

function mockRoutineResponse(overrides: Record<string, unknown> = {}) {
  return {
    member_id: 1,
    name: "Luis",
    routine: [
      {
        weekday: 2,
        name: "Piernas",
        exercises: [{ exercise: "squat", sets: 4, reps: "8-10" }],
      },
      {
        weekday: 4,
        name: "Empuje",
        exercises: [{ exercise: "bench press", sets: 3, reps: "10" }],
      },
    ],
    routine_id: 1,
    coach_authored: false,
    routine_author: null,
    routine_preset_name: null,
    catalog: ["squat", "bench press", "deadlift"],
    ...overrides,
  };
}

describe("RoutineEditor", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state while fetching", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise(() => {})
    );

    renderEditor("/members/1/routine");
    // The Loader2 renders with motion-safe:animate-spin
    const loaderContainer = document.querySelector("[class*='animate-spin']");
    expect(loaderContainer).not.toBeNull();
  });

  it("renders the editor with routine data after fetch", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Luis's routine")).toBeDefined();
    });

    // Ownership chip
    expect(screen.getByText("Agent-managed")).toBeDefined();

    // Pre-filled data
    const piernasInput = screen.getByDisplayValue("Piernas");
    expect(piernasInput).toBeDefined();
    const squatInput = screen.getByDisplayValue("squat");
    expect(squatInput).toBeDefined();
    const benchInput = screen.getByDisplayValue("bench press");
    expect(benchInput).toBeDefined();

    // Catalog
    expect(screen.getByText("Exercise catalog")).toBeDefined();

    // Save button
    expect(screen.getByText("Save Routine")).toBeDefined();
  });

  it("shows coach-authored chip when coach authored", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse({
        coach_authored: true,
        routine_author: "Coach Ana",
      })),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(
        screen.getByText("Coach-authored — Coach Ana")
      ).toBeDefined();
    });
  });

  it("shows preset chip when linked to a preset", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse({
        routine_preset_name: "Beginner",
        coach_authored: true,
        routine_author: "Coach Ana",
      })),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Preset: Beginner")).toBeDefined();
    });
  });

  it("handles member not found", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: () => Promise.resolve({}),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Member not found.")).toBeDefined();
    });
  });

  it("submits the form and shows success", async () => {
    const user = userEvent.setup();
    const routineData = mockRoutineResponse();
    const saveResponse = {
      ok: true,
      routine_id: 2,
      routine: [
        {
          weekday: 2,
          name: "Piernas",
          exercises: [{ exercise: "squat", sets: 4, reps: "8-10" }],
        },
      ],
      coach_authored: true,
      routine_author: "Coach Ana",
      routine_preset_name: null,
      notified: true,
    };

    // GET (initial) + PUT (save) + GET (invalidation refetch)
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(saveResponse),
      } as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Save Routine")).toBeDefined();
    });

    // Remove the second day (Empuje)
    const trashButtons = screen.getAllByLabelText("Remove day");
    expect(trashButtons.length).toBe(2);
    await user.click(trashButtons[1]);

    // Click save
    await user.click(screen.getByText("Save Routine"));

    await waitFor(() => {
      expect(screen.getByText("Routine saved.")).toBeDefined();
    });
    // notified: true -> the banner names the Member.
    expect(screen.getByText("We told Luis.")).toBeInTheDocument();
  });

  it("omits the notified suffix when the API says nobody was told (P2, PR review)", async () => {
    const user = userEvent.setup();
    const routineData = mockRoutineResponse();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () =>
          Promise.resolve({
            ok: true,
            routine_id: 2,
            routine: routineData.routine,
            coach_authored: true,
            routine_author: "Coach Ana",
            routine_preset_name: null,
            notified: false,
          }),
      } as Response)
      .mockResolvedValue({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response);

    renderEditor("/members/1/routine");
    await waitFor(() => {
      expect(screen.getByText("Save Routine")).toBeDefined();
    });

    await user.click(screen.getByText("Save Routine"));

    await waitFor(() => {
      expect(screen.getByText("Routine saved.")).toBeDefined();
    });
    expect(screen.queryByText(/We told/)).not.toBeInTheDocument();
  });

  it("shows stale refusal with fresh version on screen", async () => {
    const user = userEvent.setup();
    const routineData = mockRoutineResponse();
    const staleResponse = {
      error: "This routine changed while you were editing.",
      fresh_routine: [
        {
          weekday: 0,
          name: "New plan",
          exercises: [{ exercise: "deadlift", sets: 3, reps: "5" }],
        },
      ],
      fresh_routine_id: 2,
    };

    // GET (initial) + PUT (stale) — no invalidation on error
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response)
      .mockResolvedValueOnce({
        ok: false, status: 409,
        json: () => Promise.resolve(staleResponse),
      } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Save Routine")).toBeDefined();
    });

    // Edit the workout name before saving — this is the value that must
    // survive the 409 refusal (not just pre-loaded server data).
    const nameInput = screen.getByDisplayValue("Piernas");
    await user.clear(nameInput);
    await user.type(nameInput, "COACH-EDIT-CustomName");

    await user.click(screen.getByText("Save Routine"));

    await waitFor(() => {
      expect(
        screen.getByText("This routine changed while you were editing.")
      ).toBeDefined();
      expect(screen.getByText("Current version")).toBeDefined();
      expect(screen.getByText("New plan")).toBeDefined();
      expect(screen.getByText("deadlift")).toBeDefined();
    });

    // The user's own edits stay on screen (not destroyed by refusal).
    // Both the pre-loaded exercise (squat) and the edited name survive.
    expect(screen.getByDisplayValue("squat")).toBeDefined();
    expect(screen.getByDisplayValue("COACH-EDIT-CustomName")).toBeDefined();
  });

  it("shows server validation errors", async () => {
    const user = userEvent.setup();
    const routineData = mockRoutineResponse();
    const errorResponse = {
      error: "Every day needs at least one exercise.",
    };

    // GET + PUT (error) — no invalidation on error
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(routineData),
      } as Response)
      .mockResolvedValueOnce({
        ok: false, status: 400,
        json: () => Promise.resolve(errorResponse),
      } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Save Routine")).toBeDefined();
    });

    await user.click(screen.getByText("Save Routine"));

    await waitFor(() => {
      expect(
        screen.getByText("Every day needs at least one exercise.")
      ).toBeDefined();
    });
  });

  it("adds a new day block with the Add day button", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Save Routine")).toBeDefined();
    });

    const addBtn = screen.getByText("Add day");
    await user.click(addBtn);

    // Third day block appears
    await waitFor(() => {
      const dayBlocks = document.querySelectorAll("fieldset");
      expect(dayBlocks.length).toBe(3);
    });
  });

  it("shows catalog chips when catalog section is opened", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Exercise catalog")).toBeDefined();
    });

    // Open catalog details
    await user.click(screen.getByText("Exercise catalog"));

    // Catalog chips are visible
    expect(screen.getByText("deadlift")).toBeDefined();
  });

  it("fills the first empty exercise from the catalog through form state", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");
    const exerciseInputs = await screen.findAllByPlaceholderText("squat");
    await user.clear(exerciseInputs[0]);
    await user.click(screen.getByText("Exercise catalog"));
    await user.click(screen.getByRole("button", { name: "deadlift" }));

    expect(exerciseInputs[0]).toHaveValue("deadlift");
  });

  // i18n: Spanish weekday names (issue #151, review 2).
  it("renders Spanish weekday names from window.__I18N__._weekdays", async () => {
    (window as any).__I18N__ = {
      ...EN_BOOTSTRAP,
      _weekdays: ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
      pick_day: "\u2014 día \u2014",
    };

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      // Weekday 2 = Wednesday = "miércoles" in Spanish
      expect(screen.getAllByText("miércoles").length).toBeGreaterThanOrEqual(1);
      // Weekday 4 = Friday = "viernes" in Spanish
      expect(screen.getAllByText("viernes").length).toBeGreaterThanOrEqual(1);
    });
  });

  // i18n: translated error strings (issue #151, review 2).
  it("renders the member_not_found string via useT", async () => {
    (window as any).__I18N__ = {
      ...EN_BOOTSTRAP,
      member_not_found: "Miembro no encontrado.",
    };

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: () => Promise.resolve({}),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      expect(screen.getByText("Miembro no encontrado.")).toBeDefined();
    });
  });

  // Regression: prove the i18n bites by reverting a string to English.
  it("shows English when _weekdays is missing (fallback path)", async () => {
    // No _weekdays key — getWeekdays() falls back to English.
    (window as any).__I18N__ = { ...EN_BOOTSTRAP };
    delete (window as any).__I18N__._weekdays;

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockRoutineResponse()),
    } as Response);

    renderEditor("/members/1/routine");

    await waitFor(() => {
      // Falls back to English weekday names.
      expect(screen.getAllByText("Wednesday").length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText("Friday").length).toBeGreaterThanOrEqual(1);
    });
  });

  // WCAG AA contrast: save button must use text-bg on bg-magenta (issue #218).
  describe("save button contrast (issue #218)", () => {
    it("renders with text-bg on the default-state save button", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve(mockRoutineResponse()),
      } as Response);

      renderEditor("/members/1/routine");

      await waitFor(() => {
        expect(screen.getByText("Save Routine")).toBeDefined();
      });

      const btn = screen.getByRole("button", { name: "Save Routine" });

      // --- base-background: parse exact classList tokens so variant- -----
      //     prefixed utilities (e.g. hover:bg-magenta) are never mistaken
      //     for the unmodified base class.
      expect(btn.classList.contains("bg-magenta")).toBe(true);
      expect(btn.classList.contains("text-bg")).toBe(true);

      // --- foreground: exactly one effective design-token colour on the --
      //     default-state button.  Classify every text-* token against the
      //     inventory derived from the live Tailwind config so arbitrary-
      //     value utilities like text-[14px] are never misclassified.
      const colorClasses = classTokens(btn.className).filter(isTextColorClass);
      expect(colorClasses).toEqual(["text-bg"]);
    });

    it("rejects conflicting text-color utilities alongside text-bg", () => {
      const src = routineEditorSource;
      // Locate the submit button block by its type="submit" attr.
      const submitIdx = src.indexOf('type="submit"');
      expect(submitIdx).not.toBe(-1);
      const block = src.slice(submitIdx, submitIdx + 400);

      // Parse every className token that looks like text-* and classify
      // with the same inventory built from the live Tailwind config.
      const allTokenRe = /\b(text-\S+)\b/g;
      const rawTokens = [...block.matchAll(allTokenRe)].map((m) => m[1]);
      const colorClasses = rawTokens.filter(isTextColorClass);

      // Exactly one text-color token on the submit button.
      expect(colorClasses).toEqual(["text-bg"]);
    });

    it("rejects modifier-only background on the submit button", () => {
      const src = routineEditorSource;
      const submitIdx = src.indexOf('type="submit"');
      expect(submitIdx).not.toBe(-1);
      const block = src.slice(submitIdx, submitIdx + 400);

      // Must have a base bg- class (bg-magenta) as a standalone class.
      // The negative lookbehind excludes variant prefixes (hover:, focus:,
      // etc.) so that hover:bg-magenta without a true base bg-magenta is
      // rejected.
      expect(block).toMatch(/(?<![\w:])bg-magenta(?![\w-])/);

      // Double-check: a variant-only background (hover:bg-magenta with no
      // base bg-magenta) must not satisfy the check above.
      // Prove the negative: a block with only hover:bg-magenta would fail.
      expect("hover:bg-magenta text-bg").not.toMatch(
        /(?<![\w:])bg-magenta(?![\w-])/
      );
    });
  });

  // --- Synthetic / helper tests: prove the low-level guards work --------
  //     independently of rendering, so future edits to the component
  //     can't silently weaken the contrast requirements.
  describe("contrast helpers (unit)", () => {
    describe("isTextColorClass", () => {
      it("recognises project design-token text-colour classes", () => {
        expect(isTextColorClass("text-bg")).toBe(true);
        expect(isTextColorClass("text-magenta")).toBe(true);
        expect(isTextColorClass("text-magenta-tint")).toBe(true);
        expect(isTextColorClass("text-ink")).toBe(true);
        expect(isTextColorClass("text-ink-2")).toBe(true);
        expect(isTextColorClass("text-elevation-0")).toBe(true);
        expect(isTextColorClass("text-elevation-0-stroke")).toBe(true);
      });

      it("recognises standard Tailwind colour keywords", () => {
        expect(isTextColorClass("text-white")).toBe(true);
        expect(isTextColorClass("text-black")).toBe(true);
        expect(isTextColorClass("text-current")).toBe(true);
        expect(isTextColorClass("text-transparent")).toBe(true);
        expect(isTextColorClass("text-inherit")).toBe(true);
      });

      it("recognises standard Tailwind palette shades", () => {
        expect(isTextColorClass("text-red-500")).toBe(true);
        expect(isTextColorClass("text-blue-100")).toBe(true);
        expect(isTextColorClass("text-slate-950")).toBe(true);
      });

      it("rejects non-colour arbitrary-value classes like text-[14px]", () => {
        expect(isTextColorClass("text-[14px]")).toBe(false);
        expect(isTextColorClass("text-[--custom]")).toBe(false);
        // Typed arbitrary values with a non-colour type
        expect(isTextColorClass("text-[font-size:14px]")).toBe(false);
        expect(isTextColorClass("text-[line-height:1.5]")).toBe(false);
      });

      it("recognises typed arbitrary-value colour classes", () => {
        // Typed color: literal
        expect(isTextColorClass("text-[color:#fff]")).toBe(true);
        expect(isTextColorClass("text-[color:#f00]")).toBe(true);
        expect(isTextColorClass("text-[color:#ffffff]")).toBe(true);
        // Typed color: CSS variable
        expect(isTextColorClass("text-[color:var(--foreground)]")).toBe(true);
        expect(isTextColorClass("text-[color:var(--color-red-500)]")).toBe(true);
        // Typed color: CSS function
        expect(isTextColorClass("text-[color:rgb(255,0,0)]")).toBe(true);
        expect(isTextColorClass("text-[color:hsl(0,100%,50%)]")).toBe(true);
        expect(isTextColorClass("text-[color:oklch(0.5_0.2_180)]")).toBe(true);
      });

      it("recognises typed arbitrary-value colour classes with alpha modifier", () => {
        expect(isTextColorClass("text-[color:#fff]/50")).toBe(true);
        expect(isTextColorClass("text-[color:var(--foreground)]/75")).toBe(true);
      });

      it("recognises arbitrary-value colour classes", () => {
        expect(isTextColorClass("text-[#f00]")).toBe(true);
        expect(isTextColorClass("text-[#fff]")).toBe(true);
        expect(isTextColorClass("text-[#ffffff]")).toBe(true);
        expect(isTextColorClass("text-[#000000]")).toBe(true);
        expect(isTextColorClass("text-[rgb(255,0,0)]")).toBe(true);
        expect(isTextColorClass("text-[hsl(0,100%,50%)]")).toBe(true);
      });

      it("recognises arbitrary-value colour classes with alpha modifier", () => {
        expect(isTextColorClass("text-[#fff]/50")).toBe(true);
        expect(isTextColorClass("text-[#ffffff]/10")).toBe(true);
      });

      it("rejects typography / non-colour text-* utilities", () => {
        expect(isTextColorClass("text-left")).toBe(false);
        expect(isTextColorClass("text-sm")).toBe(false);
        expect(isTextColorClass("text-lg")).toBe(false);
        expect(isTextColorClass("text-ellipsis")).toBe(false);
        expect(isTextColorClass("text-nowrap")).toBe(false);
        expect(isTextColorClass("text-opacity-50")).toBe(false);
      });

      // P2, PR review: slash-opacity colour variants
      it("recognises slash-opacity colour variants", () => {
        // Standard keywords with opacity
        expect(isTextColorClass("text-white/50")).toBe(true);
        expect(isTextColorClass("text-black/10")).toBe(true);
        expect(isTextColorClass("text-current/75")).toBe(true);
        expect(isTextColorClass("text-transparent/0")).toBe(true);

        // Project design tokens with opacity
        expect(isTextColorClass("text-bg/50")).toBe(true);
        expect(isTextColorClass("text-magenta/25")).toBe(true);
        expect(isTextColorClass("text-ink/80")).toBe(true);
        expect(isTextColorClass("text-ink-2/60")).toBe(true);

        // Standard palette with opacity
        expect(isTextColorClass("text-red-500/25")).toBe(true);
        expect(isTextColorClass("text-slate-950/5")).toBe(true);
      });

      it("rejects non-colour text-* utilities that resemble slash-opacity", () => {
        // text-opacity-50 is a standalone non-colour utility, not a slash variant
        expect(isTextColorClass("text-opacity-50")).toBe(false);
        // Arbitrary non-colour values with a slash should still be rejected
        expect(isTextColorClass("text-[14px]/50")).toBe(false);
        expect(isTextColorClass("text-[font-size:14px]/50")).toBe(false);
      });

      // P2, fix-r3: arbitrary bracket opacity variants
      it("recognises slash-opacity colour variants with arbitrary bracket opacity", () => {
        // Standard keywords with arbitrary opacity
        expect(isTextColorClass("text-white/[.5]")).toBe(true);
        expect(isTextColorClass("text-black/[0.5]")).toBe(true);
        expect(isTextColorClass("text-current/[.25]")).toBe(true);

        // Project design tokens with arbitrary opacity
        expect(isTextColorClass("text-bg/[.5]")).toBe(true);
        expect(isTextColorClass("text-magenta/[.25]")).toBe(true);
        expect(isTextColorClass("text-ink/[.8]")).toBe(true);
        expect(isTextColorClass("text-ink-2/[.6]")).toBe(true);

        // Standard palette with arbitrary opacity
        expect(isTextColorClass("text-red-500/[.5]")).toBe(true);
        expect(isTextColorClass("text-slate-950/[.05]")).toBe(true);

        // Arbitrary-value colour classes with arbitrary opacity modifier
        expect(isTextColorClass("text-[#fff]/[.5]")).toBe(true);
        expect(isTextColorClass("text-[color:var(--foreground)]/[.75]")).toBe(true);

        // Non-colour arbitrary values with bracket opacity must still be rejected
        expect(isTextColorClass("text-[14px]/[.5]")).toBe(false);
        expect(isTextColorClass("text-[font-size:14px]/[.5]")).toBe(false);
      });

      it("rejects conflicting arbitrary-opacity text-color tokens", () => {
        // text-bg text-white/[.5] must be flagged as a conflict — the
        // arbitrary-opacity utility is still a colour class.
        const tokens = classTokens("text-bg text-white/[.5] bg-magenta");
        const colorClasses = tokens.filter(isTextColorClass);
        expect(colorClasses).toHaveLength(2);
        expect(colorClasses).toContain("text-bg");
        expect(colorClasses).toContain("text-white/[.5]");

        // Same with a project token + arbitrary opacity
        const tokens2 = classTokens("text-bg text-bg/[.5] bg-magenta");
        const colorClasses2 = tokens2.filter(isTextColorClass);
        expect(colorClasses2).toHaveLength(2);
        expect(colorClasses2).toContain("text-bg");
        expect(colorClasses2).toContain("text-bg/[.5]");
      });
    });

    it("rejects modifier-only background (lookbehind guard)", () => {
      // The lookbehind regex must only match an unmodified base utility.
      const baseBgRe = /(?<![\w:])bg-magenta(?![\w-])/;

      // Valid: standalone bg-magenta
      expect("bg-magenta text-bg").toMatch(baseBgRe);

      // Invalid: variant-prefixed without a base
      expect("hover:bg-magenta text-bg").not.toMatch(baseBgRe);
      expect("focus:bg-magenta").not.toMatch(baseBgRe);
      expect("disabled:bg-magenta").not.toMatch(baseBgRe);
    });

    it("rejects conflicting text-color tokens on the same element", () => {
      // A className with two text-colour tokens must be flagged.
      const tokens = classTokens("text-bg text-white bg-magenta");
      const colorClasses = tokens.filter(isTextColorClass);
      // This is a positive signal: the synthetic conflict is detected.
      expect(colorClasses).toHaveLength(2);
      expect(colorClasses).toContain("text-bg");
      expect(colorClasses).toContain("text-white");
    });

    it("rejects conflicting slash-opacity text-color tokens on the same element", () => {
      // A className with text-bg and a slash-opacity colour variant must be
      // flagged — the opacity-modified utility is still a colour class (P2, PR review).
      const tokens = classTokens("text-bg text-white/50 bg-magenta");
      const colorClasses = tokens.filter(isTextColorClass);
      expect(colorClasses).toHaveLength(2);
      expect(colorClasses).toContain("text-bg");
      expect(colorClasses).toContain("text-white/50");

      // Same with a project token slash-opacity variant
      const tokens2 = classTokens("text-bg text-bg/50 bg-magenta");
      const colorClasses2 = tokens2.filter(isTextColorClass);
      expect(colorClasses2).toHaveLength(2);
      expect(colorClasses2).toContain("text-bg");
      expect(colorClasses2).toContain("text-bg/50");
    });
  });

  // Reduced-motion: every animation class must be guarded (issue #151, review 3).
  describe("reduced-motion guard", () => {
    it("every animate-spin and transition- class in RoutineEditor is prefixed with motion-safe:", () => {
      const src = routineEditorSource;
      // Find every className="..." containing animate-spin or transition-
      // and assert each has motion-safe: before the animation class.
      const re = /className=(?:"([^"]*)"|\{`([^`]*)`\})/g;
      let match;
      const violations: string[] = [];
      while ((match = re.exec(src)) !== null) {
        const classes = match[1] ?? match[2] ?? "";
        const hasAnim = /\banimate-spin\b/.test(classes);
        const hasTrans = /\btransition-/.test(classes);
        if (hasAnim || hasTrans) {
          const guarded =
            (!hasAnim || /\bmotion-safe:animate-spin\b/.test(classes)) &&
            (!hasTrans || /\bmotion-safe:transition-/.test(classes));
          if (!guarded) {
            violations.push(classes);
          }
        }
      }
      expect(violations).toEqual([]);
    });
  });
});

// --- Preset master mode (#154): the same editor pointed at a Preset ---

function renderPresetEditor(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route
              path="/presets/:presetId/routine"
              element={<RoutineEditor preset />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    ),
    queryClient,
  };
}

function mockPresetRoutineResponse(overrides: Record<string, unknown> = {}) {
  return {
    preset_id: 7,
    name: "Beginner",
    routine: [
      {
        weekday: 0,
        name: "Preset day",
        exercises: [{ exercise: "squat", sets: 3, reps: "10" }],
      },
    ],
    routine_id: 42,
    routine_author: "Coach Ana",
    catalog: ["squat", "bench press"],
    ...overrides,
  };
}

describe("RoutineEditor preset mode", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    (window as any).__I18N__ = {
      ...EN_BOOTSTRAP,
      presets: "Presets",
      preset_editor_title: "Preset: {name}",
      preset_master_consequence:
        "Saving updates every Member still on this Preset.",
      preset_master_saved: "Preset saved; every linked copy is up to date.",
    };
  });

  it("fetches the master from /api/presets/{id}/routine and titles the screen", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true, status: 200,
      json: () => Promise.resolve(mockPresetRoutineResponse()),
    } as Response);

    renderPresetEditor("/presets/7/routine");

    await waitFor(() => {
      expect(screen.getByText("Preset: Beginner")).toBeInTheDocument();
    });
    expect(fetchSpy).toHaveBeenCalledWith("/api/presets/7/routine");
    // The consequence line: editing a master touches every linked Member.
    expect(
      screen.getByText("Saving updates every Member still on this Preset.")
    ).toBeInTheDocument();
    // The way back leads to the Presets screen, not a member page.
    const back = screen.getByRole("link", { name: /Presets/ });
    expect(back).toHaveAttribute("href", "/presets");
    // The master's day is loaded into the form.
    expect(screen.getByDisplayValue("Preset day")).toBeInTheDocument();
  });

  it("saves through PUT /api/presets/{id}/routine with the stale-check stamp", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(mockPresetRoutineResponse()),
      } as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () =>
          Promise.resolve({
            ok: true,
            preset_id: 7,
            name: "Beginner",
            routine: [
              {
                weekday: 0,
                name: "Preset day",
                exercises: [{ exercise: "squat", sets: 3, reps: "10" }],
              },
            ],
            routine_id: 43,
            routine_author: "Coach Ana",
            notified: 2,
          }),
      } as Response)
      .mockResolvedValue({
        // The post-save invalidation refetches the master.
        ok: true, status: 200,
        json: () => Promise.resolve(mockPresetRoutineResponse({ routine_id: 43 })),
      } as Response);

    renderPresetEditor("/presets/7/routine");
    await waitFor(() => {
      expect(screen.getByText("Preset: Beginner")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "Save Routine" }));

    await waitFor(() => {
      // The preset save has its own copy - and never the member-notified
      // suffix naming the preset as if it were a person (P2, PR review).
      expect(
        screen.getByText("Preset saved; every linked copy is up to date.")
      ).toBeInTheDocument();
    });
    expect(screen.queryByText(/We told/)).not.toBeInTheDocument();
    const [url, init] = fetchSpy.mock.calls[1];
    expect(url).toBe("/api/presets/7/routine");
    expect(init?.method).toBe("PUT");
    const body = JSON.parse(String(init?.body));
    expect(body.base_routine_id).toBe(42); // the stale-check stamp
    expect(body.workouts[0].name).toBe("Preset day");
  });

  it("shows the stale refusal with the fresh master", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: () => Promise.resolve(mockPresetRoutineResponse()),
      } as Response)
      .mockResolvedValueOnce({
        ok: false, status: 409,
        json: () =>
          Promise.resolve({
            error: "This routine changed while you were editing.",
            fresh_routine: [
              {
                weekday: 1,
                name: "Newer day",
                exercises: [{ exercise: "squat", sets: 5, reps: "5" }],
              },
            ],
            fresh_routine_id: 99,
          }),
      } as Response);

    renderPresetEditor("/presets/7/routine");
    await waitFor(() => {
      expect(screen.getByText("Preset: Beginner")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "Save Routine" }));

    await waitFor(() => {
      expect(
        screen.getByText("This routine changed while you were editing.")
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/Newer day/)).toBeInTheDocument();
  });
});
