import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SettingsPage } from "../components/SettingsPage";

// Mock window.__I18N__ with ES strings (the no-signal default)
const MOCK_I18N: Record<string, string> = {
  settings_title: "Ajustes",
  invite_section: "Enlace de invitación",
  invite_blurb: "El que usan los nuevos miembros para unirse a",
  coach_section: "Enlace para coaches",
  coach_blurb:
    "Privado: reenvíaselo solo a quien quieras sumar como coach.",
  gym_name_section: "Nombre del gimnasio",
  gym_name_help: "Es el nombre que ven los miembros al unirse.",
  copy: "Copiar",
  copied: "Copiado",
  regenerate: "Regenerar",
  confirm_word: "regenerar",
  confirm_prompt: "Escribe <b>{word}</b> para confirmar:",
  confirm_mismatch:
    "Escribe <b>{word}</b> para confirmar la regeneración.",
  invite_warning:
    "Regenerar el enlace invalida el código actual — quien esté a mitad de vincularse tendrá que empezar de nuevo con el enlace nuevo.",
  coach_warning:
    "Regenerar el enlace de coach invalida el código actual. Los coaches que ya se vincularon conservan su acceso.",
  gym_name_empty: "El nombre del gimnasio no puede estar vacío.",
  save: "Guardar",
  back_to_dashboard: "Volver al dashboard",
  done_link_regenerated: "Enlace regenerado.",
  done_saved: "Guardado.",
  settings: "Ajustes",
  nav_sections: "Secciones",
  presets: "Presets",
  settings_load_error: "No se pudieron cargar los ajustes.",
  nav_dashboard: "Dashboard",
};

const MOCK_SETTINGS = {
  gym_name: "Iron Temple",
  invite_code: "abc12345",
  invite_url: "https://t.me/testbot?start=abc12345",
  qr_svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100"/></svg>',
  coach_invite_code: "coach-xyz99",
  coach_invite_url: "https://t.me/testbot?start=coach-xyz99",
  bot_username: "testbot",
};

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  (window as unknown as Record<string, unknown>).__I18N__ = MOCK_I18N;
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__I18N__;
  });

  it("shows loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise<Response>(() => {
          /* never resolves */
        }),
    );

    renderWithProviders(<SettingsPage />);
    expect(screen.getByText("Loading…")).toBeDefined();
  });

  it("renders all five sections with settings data", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(MOCK_SETTINGS),
    } as Response);

    renderWithProviders(<SettingsPage />);

    // Wait for data to load — gym name appears in the chrome and invite blurb
    await waitFor(() => {
      const matches = screen.getAllByText("Iron Temple");
      expect(matches.length).toBeGreaterThanOrEqual(2);
    });

    // Five distinct sections
    expect(screen.getByText("Enlace de invitación")).toBeDefined();
    expect(screen.getByText("Enlace para coaches")).toBeDefined();
    expect(screen.getByText("Nombre del gimnasio")).toBeDefined();

    // Two Regenerate headings
    const regenerateButtons = screen.getAllByText("Regenerar");
    expect(regenerateButtons.length).toBe(2);

    // Gym name input pre-filled
    const nameInput = screen.getByDisplayValue("Iron Temple");
    expect(nameInput).toBeDefined();

    // QR SVG rendered
    const container = screen.getByText("Enlace de invitación").closest("section");
    expect(container?.innerHTML).toContain("<svg");

    // Both invite URLs visible
    expect(
      screen.getByText((content) =>
        content.includes("t.me/testbot?start=abc12345"),
      ),
    ).toBeDefined();
    expect(
      screen.getByText((content) =>
        content.includes("t.me/testbot?start=coach-xyz99"),
      ),
    ).toBeDefined();

    // Back link
    expect(
      screen.getByText((content) => content.includes("Volver al dashboard")),
    ).toBeDefined();
  });

  it("regenerate buttons are disabled until confirm word is typed", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(MOCK_SETTINGS),
    } as Response);

    renderWithProviders(<SettingsPage />);
    await waitFor(() => {
      const matches = screen.getAllByText("Iron Temple");
      expect(matches.length).toBeGreaterThanOrEqual(2);
    });

    // Both regenerate buttons start disabled
    const regenerateButtons = screen.getAllByText("Regenerar");
    for (const btn of regenerateButtons) {
      expect((btn.closest("button") as HTMLButtonElement).disabled).toBe(true);
    }

    // Two confirm inputs
    const confirmInputs = screen.getAllByPlaceholderText("regenerar");
    expect(confirmInputs.length).toBe(2);

    // Save button for gym name
    const saveButton = screen.getByText("Guardar");
    expect(saveButton).toBeDefined();
  });

  it("copy buttons are present for both invite links", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(MOCK_SETTINGS),
    } as Response);

    renderWithProviders(<SettingsPage />);
    await waitFor(() => {
      const matches = screen.getAllByText("Iron Temple");
      expect(matches.length).toBeGreaterThanOrEqual(2);
    });

    const copyButtons = screen.getAllByText("Copiar");
    expect(copyButtons.length).toBe(2);
  });

  it("shows error state when fetch fails", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new Error("Network error"),
    );

    renderWithProviders(<SettingsPage />);

    // Error state shows the real copy, not a roster empty-state string
    await waitFor(() => {
      expect(screen.getByText("No se pudieron cargar los ajustes.")).toBeDefined();
    });
  });
});
