import { useState, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../hooks/useT";
import {
  fetchPresets,
  createPreset,
  applyPreset,
  toggleDefaultPreset,
  retirePreset,
  PresetsApiError,
} from "../api/presets";
import type { Preset, PresetMember } from "../types/presets";

/** The full Presets management screen (issue #152). */
export function PresetsPage() {
  const t = useT();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["presets"],
    queryFn: fetchPresets,
  });

  const [createName, setCreateName] = useState("");
  const [createError, setCreateError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  // Track per-card apply member selection.
  const [selectedMembers, setSelectedMembers] = useState<
    Record<number, number[]>
  >({});

  const clearMsg = useCallback(() => {
    setSuccessMsg("");
    setCreateError("");
  }, []);

  const createMutation = useMutation({
    mutationFn: (name: string) => createPreset(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      setCreateName("");
      setCreateError("");
      setSuccessMsg(t("done_preset_created"));
    },
    onError: (err: Error) => {
      if (err instanceof PresetsApiError) {
        setCreateError(t(err.data.error) || err.data.error);
      } else {
        setCreateError(err.message);
      }
    },
  });

  const applyMutation = useMutation({
    mutationFn: ({
      presetId,
      memberIds,
      applyAll,
    }: {
      presetId: number;
      memberIds: number[];
      applyAll: boolean;
    }) => applyPreset(presetId, memberIds, applyAll),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      setSuccessMsg(
        t("done_preset_applied") + ` (${data.applied} ${t("apply_members").toLowerCase()})`,
      );
    },
    onError: (err: Error) => {
      setSuccessMsg("");
      if (err instanceof PresetsApiError) {
        setCreateError(t(err.data.error) || err.data.error);
      } else {
        setCreateError(err.message);
      }
    },
  });

  const defaultMutation = useMutation({
    mutationFn: (presetId: number) => toggleDefaultPreset(presetId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      setSuccessMsg(
        data.default_preset_id !== null
          ? t("done_default_set")
          : t("done_default_cleared"),
      );
    },
    onError: (err: Error) => {
      if (err instanceof PresetsApiError) {
        setCreateError(t(err.data.error) || err.data.error);
      }
    },
  });

  const retireMutation = useMutation({
    mutationFn: (presetId: number) => retirePreset(presetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      setSuccessMsg(t("done_preset_retired"));
    },
    onError: (err: Error) => {
      if (err instanceof PresetsApiError) {
        setCreateError(t(err.data.error) || err.data.error);
      }
    },
  });

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    clearMsg();
    createMutation.mutate(createName);
  };

  const handleApply = (presetId: number) => {
    clearMsg();
    const ids = selectedMembers[presetId] ?? [];
    applyMutation.mutate({ presetId, memberIds: ids, applyAll: false });
  };

  const handleApplyAll = (presetId: number) => {
    clearMsg();
    applyMutation.mutate({ presetId, memberIds: [], applyAll: true });
  };

  const handleDefault = (presetId: number) => {
    clearMsg();
    defaultMutation.mutate(presetId);
  };

  const handleRetire = (presetId: number) => {
    if (!window.confirm(t("retire_confirm"))) return;
    clearMsg();
    retireMutation.mutate(presetId);
  };

  const toggleMember = (presetId: number, memberId: number) => {
    setSelectedMembers((prev) => {
      const current = prev[presetId] ?? [];
      if (current.includes(memberId)) {
        return { ...prev, [presetId]: current.filter((id) => id !== memberId) };
      }
      return { ...prev, [presetId]: [...current, memberId] };
    });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-2">
        {t("presets_loading")}
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[200px] text-ink-2 gap-4">
        <p>{t("presets_error")}</p>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 rounded bg-elevation-1 border border-elevation-0-stroke text-ink hover:bg-elevation-2 transition-colors"
        >
          {t("presets_retry")}
        </button>
      </div>
    );
  }

  const { presets, members } = data;

  return (
    <div className="max-w-2xl mx-auto px-gut py-6">
      {/* Messages */}
      {successMsg && (
        <p className="px-3 py-2 mb-4 rounded text-sm bg-elevation-1 border border-elevation-0-stroke text-ink">
          {successMsg}
        </p>
      )}
      {createError && (
        <p className="px-3 py-2 mb-4 rounded text-sm text-ink bg-elevation-1 border border-coral">
          {createError}
        </p>
      )}

      {/* Create preset form */}
      <section className="mb-8">
        <h2 className="text-[13px] uppercase tracking-widest text-ink-2 mb-3">
          {t("create_preset")}
        </h2>
        <form onSubmit={handleCreate} className="flex gap-2">
          <label className="sr-only" htmlFor="preset-name">
            {t("preset_name")}
          </label>
          <input
            id="preset-name"
            type="text"
            value={createName}
            onChange={(e) => {
              setCreateName(e.target.value);
              if (createError) setCreateError("");
            }}
            placeholder={t("preset_name")}
            maxLength={100}
            required
            className="flex-1 h-10 px-3 bg-elevation-1 border border-elevation-0-stroke rounded text-ink text-sm placeholder:text-ink-3 
                       focus:outline-none focus:border-magenta"
          />
          <button
            type="submit"
            disabled={createMutation.isPending}
            className="h-10 px-4 bg-magenta text-black rounded text-sm font-medium 
                       hover:brightness-110 disabled:opacity-50 transition-colors"
          >
            {t("create_preset")}
          </button>
        </form>
      </section>

      {/* Preset cards */}
      {presets.length === 0 ? (
        <div className="text-center py-12 text-ink-2">
          <div className="text-3xl mb-3" aria-hidden="true">
            ◎
          </div>
          <h2 className="text-lg">{t("no_presets")}</h2>
        </div>
      ) : (
        <div className="space-y-6">
          {presets.map((preset) => (
            <PresetCard
              key={preset.id}
              preset={preset}
              members={members}
              selectedMemberIds={selectedMembers[preset.id] ?? []}
              onToggleMember={(memberId) => toggleMember(preset.id, memberId)}
              onApply={() => handleApply(preset.id)}
              onApplyAll={() => handleApplyAll(preset.id)}
              onDefault={() => handleDefault(preset.id)}
              onRetire={() => handleRetire(preset.id)}
              isApplying={applyMutation.isPending}
              isDefaulting={defaultMutation.isPending}
              isRetiring={retireMutation.isPending}
              t={t}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface PresetCardProps {
  preset: Preset;
  members: PresetMember[];
  selectedMemberIds: number[];
  onToggleMember: (memberId: number) => void;
  onApply: () => void;
  onApplyAll: () => void;
  onDefault: () => void;
  onRetire: () => void;
  isApplying: boolean;
  isDefaulting: boolean;
  isRetiring: boolean;
  t: (key: string) => string;
}

function PresetCard({
  preset,
  members,
  selectedMemberIds,
  onToggleMember,
  onApply,
  onApplyAll,
  onDefault,
  onRetire,
  isApplying,
  isDefaulting,
  isRetiring,
  t,
}: PresetCardProps) {
  const hasSelection = selectedMemberIds.length > 0;

  return (
    <section
      className={`p-4 rounded-lg border ${
        preset.is_default
          ? "border-magenta bg-elevation-1 shadow-glow-accent"
          : "border-elevation-0-stroke bg-elevation-0"
      }`}
    >
      {/* Header row */}
      <div className="flex items-center gap-2 mb-4">
        <h3 className="text-base font-semibold text-ink">{preset.name}</h3>
        {preset.is_default && (
          <span className="px-2 py-0.5 text-[11px] font-medium rounded-full bg-magenta text-black">
            {t("preset_default")}
          </span>
        )}
        <span className="flex-1" />
        <a
          href={`/presets/${preset.id}/routine`}
          className="text-[13px] text-magenta hover:brightness-110 transition-colors"
        >
          {t("edit_preset")}
        </a>
      </div>

      {/* Apply section */}
      <div className="mb-4">
        <p className="text-[11px] uppercase tracking-widest text-ink-3 mb-2">
          {t("apply_preset")}
        </p>

        {!preset.has_master ? (
          <p className="text-[13px] text-ink-3 italic">
            {t("preset_no_master")}
          </p>
        ) : members.length === 0 ? (
          <p className="text-[13px] text-ink-3 italic">
            {t("no_members_to_apply")}
          </p>
        ) : (
          <>
            {/* Member chips */}
            <div className="flex flex-wrap gap-1.5 mb-3">
              <button
                type="button"
                onClick={onApplyAll}
                disabled={isApplying}
                className="px-2.5 py-1 text-[12px] rounded-full border border-magenta text-magenta 
                           hover:bg-magenta hover:text-black disabled:opacity-50 transition-colors"
              >
                {t("apply_all")}
              </button>
              {members.map((m) => {
                const selected = selectedMemberIds.includes(m.id);
                return (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => onToggleMember(m.id)}
                    disabled={isApplying}
                    className={`px-2.5 py-1 text-[12px] rounded-full border transition-colors disabled:opacity-50 ${
                      selected
                        ? "bg-magenta border-magenta text-black"
                        : "border-elevation-0-stroke text-ink-2 hover:border-ink-2"
                    }`}
                  >
                    {m.name}
                  </button>
                );
              })}
            </div>

            {/* Apply button */}
            <button
              type="button"
              onClick={onApply}
              disabled={!hasSelection || isApplying}
              className="h-8 px-4 bg-magenta text-black rounded text-[13px] font-medium 
                         hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {t("apply")}
            </button>
          </>
        )}
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-2 pt-3 border-t border-elevation-0-stroke">
        <button
          type="button"
          onClick={onDefault}
          disabled={isDefaulting}
          className="h-8 px-3 text-[12px] rounded border border-elevation-0-stroke text-ink-2
                     hover:bg-elevation-1 hover:text-ink disabled:opacity-50 transition-colors"
        >
          {preset.is_default ? t("clear_default_preset") : t("set_default_preset")}
        </button>
        <span className="flex-1" />
        <button
          type="button"
          onClick={onRetire}
          disabled={isRetiring}
          className="h-8 px-3 text-[12px] rounded text-coral hover:bg-coral-tint disabled:opacity-50 transition-colors"
        >
          {t("retire_preset")}
        </button>
      </div>
    </section>
  );
}
