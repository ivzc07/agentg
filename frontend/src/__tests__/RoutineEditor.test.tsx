import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoutineEditor } from "../components/RoutineEditor";
// Vite serves the component's own source as a string, so the guard below reads
// production code without pulling node builtins into the typechecked build.
import routineEditorSource from "../components/RoutineEditor.tsx?raw";

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
