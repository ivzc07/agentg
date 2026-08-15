import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { PresetsPage } from "../components/PresetsPage";
import type { PresetsResponse } from "../types/presets";

// Mock useT with preset-related English strings.
vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      create_preset: "Create preset",
      preset_name: "Preset name",
      preset_name_empty: "The preset name cannot be empty.",
      preset_name_too_long: "The preset name cannot exceed 100 characters.",
      duplicate_preset_name: "A preset with that name already exists in this gym.",
      no_presets: "No presets yet.",
      edit_preset: "Edit",
      apply_preset: "Apply preset",
      apply_members: "Members",
      apply_all: "All members",
      apply: "Apply",
      no_members_to_apply: "There are no members to apply it to.",
      preset_no_master: "Write the preset's plan before applying it.",
      preset_no_selection: "Pick at least one member.",
      preset_default: "Default",
      set_default_preset: "Use as default",
      clear_default_preset: "Clear default",
      retire_preset: "Retire preset",
      retire_confirm:
        "Retire this preset? Members keep their copies, but the preset can no longer be edited or applied.",
      done_preset_created: "Preset created.",
      done_preset_applied: "Preset applied.",
      done_default_set: "Default preset set.",
      done_default_cleared: "Default preset cleared.",
      done_preset_retired: "Preset retired.",
      presets_title: "Presets",
      presets_loading: "Loading…",
      presets_error: "Something went wrong loading your presets.",
      presets_retry: "Retry",
    };
    return strings[key] ?? key;
  },
}));

// Mock global fetch.
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

// Mock window.confirm
const mockConfirm = vi.fn();
vi.stubGlobal("confirm", mockConfirm);

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/presets"]}>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function makeResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  } as unknown as Response;
}

const EMPTY_RESPONSE: PresetsResponse = {
  presets: [],
  members: [],
  default_preset_id: null,
};

const ONE_PRESET: PresetsResponse = {
  presets: [
    { id: 1, name: "Beginner", is_default: false, has_master: true },
  ],
  members: [
    { id: 10, name: "Luis" },
    { id: 11, name: "Mara" },
  ],
  default_preset_id: null,
};

const DEFAULT_PRESET: PresetsResponse = {
  presets: [
    { id: 1, name: "Beginner", is_default: true, has_master: true },
    { id: 2, name: "Advanced", is_default: false, has_master: false },
  ],
  members: [
    { id: 10, name: "Luis" },
    { id: 11, name: "Mara" },
  ],
  default_preset_id: 1,
};

beforeEach(() => {
  mockFetch.mockReset();
  mockConfirm.mockReset();
  mockConfirm.mockReturnValue(true); // default: confirm
  // Default: empty presets.
  mockFetch.mockResolvedValue(makeResponse(EMPTY_RESPONSE));
});

describe("PresetsPage", () => {
  it("shows empty state when no presets exist", async () => {
    render(<PresetsPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("No presets yet.")).toBeInTheDocument();
    });
  });

  it("shows the create preset form", async () => {
    render(<PresetsPage />, { wrapper });
    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Preset name"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "Create preset" }),
    ).toBeInTheDocument();
  });

  it("creates a preset and shows success", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(EMPTY_RESPONSE)); // initial
    mockFetch.mockResolvedValueOnce(
      makeResponse({ id: 3, name: "Cutting" }, 201),
    ); // create
    mockFetch.mockResolvedValueOnce(
      makeResponse({
        presets: [{ id: 3, name: "Cutting", is_default: false, has_master: false }],
        members: [],
        default_preset_id: null,
      }),
    ); // refetch

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("No presets yet.")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Preset name");
    await user.type(input, "Cutting");
    await user.click(screen.getByRole("button", { name: "Create preset" }));

    await waitFor(() => {
      expect(screen.getByText("Preset created.")).toBeInTheDocument();
    });
  });

  it("shows error on duplicate preset name", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // initial
    mockFetch.mockResolvedValueOnce(
      makeResponse({ error: "duplicate_preset_name" }, 400),
    ); // create fails

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Preset name");
    await user.type(input, "Beginner");
    await user.click(screen.getByRole("button", { name: "Create preset" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "A preset with that name already exists in this gym.",
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows preset cards with apply form", async () => {
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET));

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    // Member chips
    expect(screen.getByText("Luis")).toBeInTheDocument();
    expect(screen.getByText("Mara")).toBeInTheDocument();
    // Apply all button
    expect(screen.getByText("All members")).toBeInTheDocument();
    // Apply button (disabled initially)
    const applyBtn = screen.getByRole("button", { name: "Apply" });
    expect(applyBtn).toBeDisabled();
  });

  it("enables apply button when members are selected", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET));

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    const luisChip = screen.getByText("Luis");
    await user.click(luisChip);

    const applyBtn = screen.getByRole("button", { name: "Apply" });
    expect(applyBtn).not.toBeDisabled();
  });

  it("applies preset to selected members", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // initial
    mockFetch.mockResolvedValueOnce(makeResponse({ applied: 1 })); // apply
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // refetch

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Luis"));
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/presets/1/apply",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ member_ids: [10], apply_all: false }),
        }),
      );
    });
  });

  it("applies preset to all members", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // initial
    mockFetch.mockResolvedValueOnce(makeResponse({ applied: 2 })); // apply
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // refetch

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    await user.click(screen.getByText("All members"));

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        "/api/presets/1/apply",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ member_ids: [], apply_all: true }),
        }),
      );
    });
  });

  it("shows no-master message for preset without master", async () => {
    mockFetch.mockResolvedValueOnce(
      makeResponse({
        presets: [
          { id: 1, name: "Empty", is_default: false, has_master: false },
        ],
        members: [{ id: 10, name: "Luis" }],
        default_preset_id: null,
      }),
    );

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(
        screen.getByText("Write the preset's plan before applying it."),
      ).toBeInTheDocument();
    });

    // No apply button or member chips shown.
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("shows default badge for default preset", async () => {
    mockFetch.mockResolvedValueOnce(makeResponse(DEFAULT_PRESET));

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    // The default preset card has a "Default" badge.
    const beginnerCard = screen.getByText("Beginner").closest("section")!;
    expect(within(beginnerCard).getByText("Default")).toBeInTheDocument();

    // The default preset has "Clear default" button.
    expect(
      within(beginnerCard).getByRole("button", { name: "Clear default" }),
    ).toBeInTheDocument();

    // Non-default preset has "Use as default".
    const advancedCard = screen.getByText("Advanced").closest("section")!;
    expect(
      within(advancedCard).getByRole("button", { name: "Use as default" }),
    ).toBeInTheDocument();
  });

  it("toggles default preset on button click", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // initial
    mockFetch.mockResolvedValueOnce(
      makeResponse({ default_preset_id: 1 }),
    ); // toggle
    mockFetch.mockResolvedValueOnce(
      makeResponse({
        ...ONE_PRESET,
        presets: [{ ...ONE_PRESET.presets[0], is_default: true }],
        default_preset_id: 1,
      }),
    ); // refetch

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    await user.click(
      screen.getByRole("button", { name: "Use as default" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Default preset set.")).toBeInTheDocument();
    });
  });

  it("retires a preset with confirmation", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET)); // initial
    mockFetch.mockResolvedValueOnce(makeResponse({ retired: true })); // retire
    mockFetch.mockResolvedValueOnce(makeResponse(EMPTY_RESPONSE)); // refetch

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Retire preset" }));

    // Confirm should have been called.
    expect(mockConfirm).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.getByText("Preset retired.")).toBeInTheDocument();
    });
  });

  it("does not retire when confirmation is cancelled", async () => {
    const user = userEvent.setup();
    mockConfirm.mockReturnValueOnce(false);
    mockFetch.mockResolvedValueOnce(makeResponse(ONE_PRESET));

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("Beginner")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Retire preset" }));

    expect(mockConfirm).toHaveBeenCalled();
    // No retire API call was made.
    const retireCalls = mockFetch.mock.calls.filter(
      (call: string[]) =>
        typeof call[0] === "string" && call[0].includes("/retire"),
    );
    expect(retireCalls).toHaveLength(0);
  });

  it("shows no-members message when gym has no members", async () => {
    mockFetch.mockResolvedValueOnce(
      makeResponse({
        presets: [
          { id: 1, name: "Beginner", is_default: false, has_master: true },
        ],
        members: [],
        default_preset_id: null,
      }),
    );

    render(<PresetsPage />, { wrapper });

    await waitFor(() => {
      expect(
        screen.getByText("There are no members to apply it to."),
      ).toBeInTheDocument();
    });
  });
});
