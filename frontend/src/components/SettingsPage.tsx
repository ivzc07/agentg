import { LangToggle } from "./LangToggle";
import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Copy, Check, RefreshCw } from "lucide-react";
import { fetchSettings, regenerateInvite, regenerateCoach, renameGym } from "../api/settings";
import { useT } from "../hooks/useT";

/**
 * Full tenant Settings screen: invite link + QR, regenerate-invite,
 * coach link, regenerate-coach, and gym name (issue #153).
 *
 * Matches the five card blocks of the server-rendered settings page
 * (spec-dashboard §Settings) using the shared Tailwind theme tokens.
 */
export function SettingsPage() {
  const t = useT();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    staleTime: 30_000,
  });

  // --- Regenerate Invite state ---
  const [inviteConfirm, setInviteConfirm] = useState("");
  const [inviteRegenerated, setInviteRegenerated] = useState(false);

  const regenerateInviteMutation = useMutation({
    mutationFn: () => regenerateInvite(inviteConfirm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setInviteConfirm("");
      setInviteRegenerated(true);
      setTimeout(() => setInviteRegenerated(false), 3000);
    },
  });

  // --- Regenerate Coach state ---
  const [coachConfirm, setCoachConfirm] = useState("");
  const [coachRegenerated, setCoachRegenerated] = useState(false);

  const regenerateCoachMutation = useMutation({
    mutationFn: () => regenerateCoach(coachConfirm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setCoachConfirm("");
      setCoachRegenerated(true);
      setTimeout(() => setCoachRegenerated(false), 3000);
    },
  });

  // --- Gym name state ---
  const [gymName, setGymName] = useState("");
  const [gymNameSaved, setGymNameSaved] = useState(false);

  // Sync local gym name state when data loads
  useEffect(() => {
    if (data) {
      setGymName(data.gym_name);
    }
  }, [data]);

  const renameGymMutation = useMutation({
    mutationFn: () => renameGym(gymName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setGymNameSaved(true);
      setTimeout(() => setGymNameSaved(false), 3000);
    },
  });

  // --- Copy to clipboard ---
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const copyUrl = (text: string, key: string) => {
    if (!navigator.clipboard) {
      return;
    }
    navigator.clipboard.writeText(text).then(
      () => {
        setCopiedKey(key);
        setTimeout(() => setCopiedKey(null), 2000);
      },
      () => {
        // silently fail — the button is a convenience
      },
    );
  };

  // --- Loading / error states ---
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-2">
        Loading…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[200px] text-coral gap-4 px-gut">
        <p>{t("settings_load_error")}</p>
      </div>
    );
  }

  const confirmWord = t("confirm_word");
  const inviteConfirmMatches =
    inviteConfirm.trim().toLowerCase() === confirmWord.toLowerCase();
  const coachConfirmMatches =
    coachConfirm.trim().toLowerCase() === confirmWord.toLowerCase();

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      {/* Top bar — matches RosterShell chrome */}
      <header className="sticky top-0 z-20 flex items-center gap-2 flex-wrap min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <h1 className="text-[17px] font-semibold tracking-[-0.01em] overflow-hidden text-ellipsis whitespace-nowrap min-w-0">
          {data.gym_name}
        </h1>
        <span className="flex-1" />
        <nav
          className="quick flex gap-2 text-[13px] text-ink-2"
          aria-label={t("nav_sections")}
        >
          <Link to="/" className="hover:text-ink transition-colors duration-fast">
            {t("nav_dashboard")}
          </Link>
          <span className="text-ink">{t("settings")}</span>
        </nav>

        <LangToggle />
      </header>

      {/* Settings body */}
      <main className="max-w-2xl mx-auto px-gut py-8 space-y-8">
        <h1 className="text-[24px] font-bold tracking-[-0.02em]">
          {t("settings_title")}
        </h1>

        {/* Success toasts */}
        {inviteRegenerated && (
          <p className="text-[14px] text-success bg-success/10 border border-success/30 px-4 py-2 rounded-sm">
            {t("done_link_regenerated")}
          </p>
        )}
        {coachRegenerated && (
          <p className="text-[14px] text-success bg-success/10 border border-success/30 px-4 py-2 rounded-sm">
            {t("done_link_regenerated")}
          </p>
        )}
        {gymNameSaved && (
          <p className="text-[14px] text-success bg-success/10 border border-success/30 px-4 py-2 rounded-sm">
            {t("done_saved")}
          </p>
        )}

        {/* Regenerate error */}
        {(regenerateInviteMutation.error as Error) && (
          <p className="text-[14px] text-coral bg-coral/10 border border-coral/30 px-4 py-2 rounded-sm">
            {(regenerateInviteMutation.error as Error).message}
          </p>
        )}
        {(regenerateCoachMutation.error as Error) && (
          <p className="text-[14px] text-coral bg-coral/10 border border-coral/30 px-4 py-2 rounded-sm">
            {(regenerateCoachMutation.error as Error).message}
          </p>
        )}
        {(renameGymMutation.error as Error) && (
          <p className="text-[14px] text-coral bg-coral/10 border border-coral/30 px-4 py-2 rounded-sm">
            {(renameGymMutation.error as Error).message}
          </p>
        )}

        {/* 1. Invite link + QR */}
        <section
          id="invite"
          className="bg-elevation-1 border border-elevation-1-stroke rounded-sm p-5 space-y-4"
        >
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">
            {t("invite_section")}
          </h2>
          <p className="text-[14px] text-ink-2">
            {t("invite_blurb")} <b>{data.gym_name}</b>.
          </p>

          {/* Invite URL + copy */}
          <div className="flex items-center gap-2 bg-elevation-0 border border-elevation-0-stroke rounded-sm px-3 py-2">
            <code className="text-[13px] text-ink-2 flex-1 break-all">
              {data.invite_url}
            </code>
            <button
              type="button"
              onClick={() => copyUrl(data.invite_url, "invite")}
              className="flex items-center gap-1 min-h-0 px-2 py-1 text-[12px] bg-elevation-2 border border-elevation-2-stroke rounded-sm hover:border-ink-2 transition-colors duration-fast"
              aria-label={t("copy")}
            >
              {copiedKey === "invite" ? (
                <Check className="w-3.5 h-3.5 text-success" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
              {copiedKey === "invite" ? t("copied") : t("copy")}
            </button>
          </div>

          {/* QR code */}
          <div
            className="flex justify-center max-w-[240px] mx-auto"
            dangerouslySetInnerHTML={{ __html: data.qr_svg }}
          />
        </section>

        {/* 2. Regenerate invite */}
        <section
          id="regenerate-invite"
          className="bg-elevation-1 border border-magenta/30 rounded-sm p-5 space-y-3"
        >
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">
            {t("regenerate")}: {t("invite_section").toLowerCase()}
          </h2>
          <p className="text-[13px] text-ink-2">{t("invite_warning")}</p>

          <div className="flex flex-col gap-2">
            <label className="text-[13px] text-ink-2">
              <span
                dangerouslySetInnerHTML={{
                  __html: t("confirm_prompt").replace(
                    "{word}",
                    `<b>${confirmWord}</b>`,
                  ),
                }}
              />
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                name="confirm"
                value={inviteConfirm}
                onChange={(e) => setInviteConfirm(e.target.value)}
                autoComplete="off"
                placeholder={confirmWord}
                className="flex-1 px-3 py-2 bg-elevation-0 border border-elevation-0-stroke rounded-sm text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 transition-colors duration-fast"
              />
              <button
                type="button"
                onClick={() => regenerateInviteMutation.mutate()}
                disabled={
                  !inviteConfirmMatches || regenerateInviteMutation.isPending
                }
                className="flex items-center gap-1.5 px-4 py-2 bg-magenta text-bg font-semibold text-[14px] rounded-sm hover:bg-magenta/90 disabled:opacity-40 disabled:cursor-default transition-colors duration-fast"
              >
                {regenerateInviteMutation.isPending ? (
                  <RefreshCw className="w-4 h-4 motion-safe:animate-spin" />
                ) : null}
                {t("regenerate")}
              </button>
            </div>
          </div>
        </section>

        {/* 3. Coach link */}
        <section
          id="coach-link"
          className="bg-elevation-1 border border-elevation-1-stroke rounded-sm p-5 space-y-4"
        >
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">
            {t("coach_section")}
          </h2>
          <p className="text-[14px] text-ink-2">{t("coach_blurb")}</p>

          <div className="flex items-center gap-2 bg-elevation-0 border border-elevation-0-stroke rounded-sm px-3 py-2">
            <code className="text-[13px] text-ink-2 flex-1 break-all">
              {data.coach_invite_url}
            </code>
            <button
              type="button"
              onClick={() => copyUrl(data.coach_invite_url, "coach")}
              className="flex items-center gap-1 min-h-0 px-2 py-1 text-[12px] bg-elevation-2 border border-elevation-2-stroke rounded-sm hover:border-ink-2 transition-colors duration-fast"
              aria-label={t("copy")}
            >
              {copiedKey === "coach" ? (
                <Check className="w-3.5 h-3.5 text-success" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
              {copiedKey === "coach" ? t("copied") : t("copy")}
            </button>
          </div>
        </section>

        {/* 4. Regenerate coach */}
        <section
          id="regenerate-coach"
          className="bg-elevation-1 border border-magenta/30 rounded-sm p-5 space-y-3"
        >
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">
            {t("regenerate")}: {t("coach_section").toLowerCase()}
          </h2>
          <p className="text-[13px] text-ink-2">{t("coach_warning")}</p>

          <div className="flex flex-col gap-2">
            <label className="text-[13px] text-ink-2">
              <span
                dangerouslySetInnerHTML={{
                  __html: t("confirm_prompt").replace(
                    "{word}",
                    `<b>${confirmWord}</b>`,
                  ),
                }}
              />
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                name="confirm"
                value={coachConfirm}
                onChange={(e) => setCoachConfirm(e.target.value)}
                autoComplete="off"
                placeholder={confirmWord}
                className="flex-1 px-3 py-2 bg-elevation-0 border border-elevation-0-stroke rounded-sm text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 transition-colors duration-fast"
              />
              <button
                type="button"
                onClick={() => regenerateCoachMutation.mutate()}
                disabled={
                  !coachConfirmMatches || regenerateCoachMutation.isPending
                }
                className="flex items-center gap-1.5 px-4 py-2 bg-magenta text-bg font-semibold text-[14px] rounded-sm hover:bg-magenta/90 disabled:opacity-40 disabled:cursor-default transition-colors duration-fast"
              >
                {regenerateCoachMutation.isPending ? (
                  <RefreshCw className="w-4 h-4 motion-safe:animate-spin" />
                ) : null}
                {t("regenerate")}
              </button>
            </div>
          </div>
        </section>

        {/* 5. Gym name */}
        <section
          id="gym-name"
          className="bg-elevation-1 border border-elevation-1-stroke rounded-sm p-5 space-y-3"
        >
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">
            {t("gym_name_section")}
          </h2>
          <p className="text-[14px] text-ink-2">{t("gym_name_help")}</p>

          <div className="flex gap-2">
            <input
              type="text"
              name="name"
              value={gymName}
              onChange={(e) => setGymName(e.target.value)}
              maxLength={200}
              className="flex-1 px-3 py-2 bg-elevation-0 border border-elevation-0-stroke rounded-sm text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 transition-colors duration-fast"
            />
            <button
              type="button"
              onClick={() => renameGymMutation.mutate()}
              disabled={
                !gymName.trim() || renameGymMutation.isPending
              }
              className="flex items-center gap-1.5 px-4 py-2 bg-ink text-bg font-semibold text-[14px] rounded-sm hover:bg-ink/90 disabled:opacity-40 disabled:cursor-default transition-colors duration-fast"
            >
              {t("save")}
            </button>
          </div>
        </section>

        {/* Back link */}
        <Link
          to="/"
          className="inline-block text-[14px] text-ink-2 hover:text-ink transition-colors duration-fast"
        >
          ← {t("back_to_dashboard")}
        </Link>
      </main>
    </div>
  );
}
