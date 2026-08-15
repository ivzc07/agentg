import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SettingsPage } from "../components/SettingsPage";

const MOCK_I18N: Record<string, string> = {
  settings_title: "Ajustes",
  settings_tab_general: "General",
  settings_tab_access: "Acceso",
  cancel: "Cancelar",
  invite_section: "Enlace de invitación",
  invite_blurb: "El que usan los nuevos miembros para unirse a",
  coach_section: "Enlace para coaches",
  coach_blurb:
    "Privado: reenvíaselo solo a quien quieras sumar como coach.",
  gym_name_section: "Nombre del gimnasio",
  gym_name_help: "Es el nombre que ven los miembros al unirse.",
  copy: "Copiar",
  copied: "Copiado",
  copy_failed: "No se pudo copiar",
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
  done_link_regenerated: "Enlace regenerado.",
  done_saved: "Guardado.",
  settings: "Ajustes",
  nav_sections: "Secciones",
  presets: "Presets",
  settings_load_error: "No se pudieron cargar los ajustes.",
  nav_dashboard: "Dashboard",
  nav_roster: "Miembros",
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
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

function mockSettings() {
  vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve(MOCK_SETTINGS),
  } as Response);
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__I18N__;
  });

  it("shows an accessible loading state initially", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise<Response>(() => {
          /* never resolves */
        }),
    );

    renderWithProviders(<SettingsPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("groups access links into composed cards", async () => {
    mockSettings();
    renderWithProviders(<SettingsPage />);

    const inviteHeading = await screen.findByRole("heading", {
      name: "Enlace de invitación",
    });
    expect(
      screen.getByRole("heading", { name: "Enlace para coaches" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Acceso" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    const inviteCard = inviteHeading.closest('[data-slot="card"]');
    expect(inviteCard).not.toBeNull();
    expect(inviteCard?.innerHTML).toContain("<svg");
    expect(
      screen.getByDisplayValue("https://t.me/testbot?start=abc12345"),
    ).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("https://t.me/testbot?start=coach-xyz99"),
    ).toBeInTheDocument();
  });

  it("uses a labeled Field inside the General tab", async () => {
    const user = userEvent.setup();
    mockSettings();
    renderWithProviders(<SettingsPage />);

    await screen.findByRole("tab", { name: "General" });
    await user.click(screen.getByRole("tab", { name: "General" }));

    const input = await screen.findByRole("textbox", {
      name: "Nombre del gimnasio",
    });
    expect(input).toHaveValue("Iron Temple");
    expect(input).toHaveAttribute("id", "gym-name");
    expect(input.closest('[data-slot="field"]')).not.toBeNull();
    expect(screen.getByRole("button", { name: "Guardar" })).toBeEnabled();
  });

  it("keeps destructive confirmation in an AlertDialog", async () => {
    const user = userEvent.setup();
    mockSettings();
    renderWithProviders(<SettingsPage />);

    await screen.findByRole("heading", { name: "Enlace de invitación" });
    const triggers = screen.getAllByRole("button", { name: "Regenerar" });
    expect(triggers).toHaveLength(2);

    await user.click(triggers[0]);
    const dialog = await screen.findByRole("alertdialog");
    const confirmInput = within(dialog).getByLabelText(
      "Escribe regenerar para confirmar:",
    );
    const action = within(dialog).getByRole("button", { name: "Regenerar" });
    expect(action).toBeDisabled();

    await user.type(confirmInput, "regenerar");
    expect(action).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
  });

  it("uses InputGroup copy actions and announces copy success", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    mockSettings();
    renderWithProviders(<SettingsPage />);

    await screen.findByRole("heading", { name: "Enlace de invitación" });
    const copyButtons = screen.getAllByRole("button", { name: "Copiar" });
    expect(copyButtons).toHaveLength(2);
    expect(document.querySelectorAll('[data-slot="input-group"]')).toHaveLength(2);

    await user.click(copyButtons[0]);
    expect(
      await screen.findByRole("button", { name: "Copiado" }),
    ).toHaveAttribute("aria-live", "polite");
  });

  it("does not overwrite an unsaved gym name when settings refetch", async () => {
    const user = userEvent.setup();
    mockSettings();
    const { queryClient } = renderWithProviders(<SettingsPage />);

    await user.click(await screen.findByRole("tab", { name: "General" }));
    const input = await screen.findByRole("textbox", {
      name: "Nombre del gimnasio",
    });
    await user.clear(input);
    await user.type(input, "Nombre sin guardar");

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
    });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    expect(input).toHaveValue("Nombre sin guardar");
  });

  it("shows a semantic alert when settings fail to load", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Network error"));
    renderWithProviders(<SettingsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No se pudieron cargar los ajustes.");
    expect(alert).toHaveAttribute("data-slot", "alert");
  });
});
