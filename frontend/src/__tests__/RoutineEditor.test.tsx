import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoutineEditor } from "../components/RoutineEditor";

beforeEach(() => {
  (window as any).__I18N__ = {
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
  };
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
    // The Loader2 renders with animate-spin
    const loaderContainer = document.querySelector(".animate-spin");
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

    await user.click(screen.getByText("Save Routine"));

    await waitFor(() => {
      expect(
        screen.getByText("This routine changed while you were editing.")
      ).toBeDefined();
      expect(screen.getByText("Current version")).toBeDefined();
      expect(screen.getByText("New plan")).toBeDefined();
      expect(screen.getByText("deadlift")).toBeDefined();
    });

    // The user's own edits stay on screen (not destroyed by refusal)
    expect(screen.getByDisplayValue("squat")).toBeDefined();
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
});
