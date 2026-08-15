import { AppHeader } from "./AppHeader";
import { useEffect, useRef, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm, useFieldArray } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useT } from "../hooks/useT";
import { getWeekdays } from "../lib/i18n";
import {
  fetchRoutine,
  saveRoutine,
  fetchPresetRoutine,
  savePresetRoutine,
} from "../api/routine";
import type {
  RoutineDay,
  RoutineSaveError,
} from "../types/routine";
import {
  Check,
  AlertTriangle,
  Loader2,
  ChevronDown,
  Plus,
  Trash2,
} from "lucide-react";

// --- Zod schema ---

const exerciseSchema = z.object({
  exercise: z.string().min(1, "Exercise name is required"),
  sets: z
    .number()
    .int()
    .min(1)
    .max(99)
    .nullable()
    .default(null),
  reps: z.string().max(40).nullable().default(null),
});

const daySchema = z.object({
  weekday: z.number().int().min(0).max(6),
  name: z.string().max(100),
  exercises: z
    .array(exerciseSchema)
    .min(1, "At least one exercise per day"),
});

const formSchema = z.object({
  base_routine_id: z.number().int().nullable(),
  workouts: z
    .array(daySchema)
    .min(1, "A routine needs at least one day")
    .refine(
      (days) => {
        const weekdays = days.map((d) => d.weekday);
        return new Set(weekdays).size === weekdays.length;
      },
      { message: "Each weekday can only appear once" }
    ),
});

type FormValues = z.infer<typeof formSchema>;

// --- Helpers ---

function defaultExercise() {
  return { exercise: "", sets: null as number | null, reps: null as string | null };
}

function defaultDay(weekday: number): RoutineDay {
  return { weekday, name: "", exercises: [defaultExercise()] };
}

function apiDayToForm(day: RoutineDay) {
  return {
    weekday: day.weekday,
    name: day.name,
    exercises: day.exercises.map((ex) => ({
      exercise: ex.exercise,
      sets: ex.sets ?? null,
      reps: ex.reps ?? null,
    })),
  };
}

function formDayToApi(
  day: FormValues["workouts"][number]
): RoutineDay {
  return {
    weekday: day.weekday,
    name: day.name,
    exercises: day.exercises.map((ex) => ({
      exercise: ex.exercise,
      sets: ex.sets as number | null,
      reps: ex.reps as string | null,
    })),
  };
}

// --- Component ---

export function RoutineEditor({ preset = false }: { preset?: boolean } = {}) {
  const { memberId, presetId } = useParams<{
    memberId: string;
    presetId: string;
  }>();
  const t = useT();
  const queryClient = useQueryClient();
  const rawId = preset ? presetId : memberId;
  const id = rawId != null ? Number(rawId) : 0;

  const formRef = useRef<HTMLFormElement>(null);
  const scrollRef = useRef<number>(0);
  const [feedback, setFeedback] = useState<{
    type: "success" | "error";
    message: string;
    /** Whether the save's Member notification actually went out. */
    notified?: boolean;
    freshRoutine?: RoutineDay[];
  } | null>(null);

  // Fetch routine data
  const {
    data,
    isLoading,
    error: fetchError,
  } = useQuery({
    queryKey: ["routine", preset ? "preset" : "member", id],
    queryFn: () => (preset ? fetchPresetRoutine(id) : fetchRoutine(id)),
    enabled: id > 0,
  });

  // Form setup
  const {
    control,
    register,
    handleSubmit,
    watch,
    reset,
    setFocus,
    setValue,
    formState: { errors },
  } = useForm<FormValues>({
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    resolver: zodResolver(formSchema) as any,
    defaultValues: {
      base_routine_id: null,
      workouts: [],
    },
  });

  const { fields, append, remove } = useFieldArray({
    control,
    name: "workouts",
  });

  const workouts = watch("workouts");

  // Populate form when data arrives
  useEffect(() => {
    if (data) {
      scrollRef.current = window.scrollY;
      reset({
        base_routine_id: data.routine_id,
        workouts:
          data.routine.length > 0
            ? data.routine.map(apiDayToForm)
            : [],
      });
    }
  }, [data, reset]);

  // Restore scroll position after reset
  useEffect(() => {
    if (scrollRef.current > 0) {
      window.scrollTo(0, scrollRef.current);
      scrollRef.current = 0;
    }
  });

  // Save mutation
  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = {
        base_routine_id: values.base_routine_id,
        workouts: values.workouts.map(formDayToApi),
      };
      return preset ? savePresetRoutine(id, body) : saveRoutine(id, body);
    },
    onSuccess: (result) => {
      if ("ok" in result && result.ok) {
        setFeedback({
          type: "success",
          // The preset master's save has its own copy (the linked copies
          // were updated); the member save names the Member — but only
          // when the notification actually went out (see the banner).
          message: preset ? t("preset_master_saved") : t("routine_saved"),
          notified: !preset && result.notified === true,
        });
        // Update form with fresh data from server
        reset({
          base_routine_id: result.routine_id,
          workouts: result.routine.map(apiDayToForm),
        });
        // Invalidate the query to get fresh data next time
        queryClient.invalidateQueries({
          queryKey: ["routine", preset ? "preset" : "member", id],
        });
      } else {
        const err = result as RoutineSaveError;
        setFeedback({
          type: "error",
          message: err.error,
          freshRoutine: err.fresh_routine,
        });
        // If stale, update base_routine_id to the fresh one
        if (err.fresh_routine_id) {
          setValue("base_routine_id", err.fresh_routine_id);
        }
      }
      // Restore scroll
      window.scrollTo(0, scrollRef.current || 0);
    },
    onError: () => {
      setFeedback({
        type: "error",
        message: t("network_error"),
      });
    },
  });

  const onSubmit = useCallback(
    (values: FormValues) => {
      scrollRef.current = window.scrollY;
      setFeedback(null);
      saveMutation.mutate(values);
    },
    [saveMutation]
  );

  const usedWeekdays = new Set(
    workouts?.map((d) => d.weekday) ?? []
  );

  const availableWeekdays = Array.from({ length: 7 }, (_, i) => i).filter(
    (w) => !usedWeekdays.has(w)
  );

  const addDay = () => {
    if (availableWeekdays.length > 0) {
      const next = availableWeekdays[0];
      append(defaultDay(next));
      setFeedback(null);
    }
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-bg flex items-center justify-center">
        <Loader2 className="w-6 h-6 motion-safe:animate-spin text-ink-2" />
      </div>
    );
  }

  if (fetchError || !data) {
    return (
      <div className="min-h-screen bg-bg flex items-center justify-center text-ink-2">
        {t("member_not_found")}
      </div>
    );
  }

  const ownerLabel = (() => {
    if (data.routine_preset_name) {
      return t("preset_chip").replace("{name}", data.routine_preset_name);
    }
    if (data.coach_authored) {
      if (data.routine_author) {
        return t("chip_coach_named").replace("{name}", data.routine_author);
      }
      return t("chip_coach");
    }
    return t("chip_agent");
  })();

  const canAddDay = availableWeekdays.length > 0;

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      <AppHeader
        showNav={false}
        leading={
          <Link
            to={preset ? "/presets" : `/members/${id}`}
            className="text-[13px] text-ink-2 hover:text-ink motion-safe:transition-colors duration-fast"
          >
            ← {preset ? t("presets") : data.name}
          </Link>
        }
      />

      <main className="max-w-2xl mx-auto px-gut py-8">
        {/* Header */}
        <div className="mb-6">
          <h1 className="text-[28px] leading-tight">
            {(preset ? t("preset_editor_title") : t("editor_title")).replace(
              "{name}",
              data.name
            )}
          </h1>
          <span className="inline-block mt-1.5 text-[13px] px-2 py-0.5 rounded-full bg-elevation-1 border border-elevation-0-stroke text-ink-2">
            {ownerLabel}
          </span>
          {preset && (
            <p className="mt-2 text-[13px] text-ink-2">
              {t("preset_master_consequence")}
            </p>
          )}
          {!preset && !data.coach_authored && !data.routine_preset_name && data && (
            <p className="mt-2 text-[13px] text-ink-2">
              {t("chip_consequence")}
            </p>
          )}
          {!preset && data && data.routine_preset_name && (
            <p className="mt-2 text-[13px] text-ink-2">
              {t("chip_consequence")}
            </p>
          )}
        </div>

        {/* Feedback: success */}
        {feedback?.type === "success" && (
          <div
            role="status"
            className="mb-6 flex items-center gap-2 px-4 py-3 rounded-sm bg-success-tint border border-success/30 text-success text-[14px]"
          >
            <Check className="w-4 h-4 flex-shrink-0" />
            <span>{feedback.message}</span>
            {feedback.notified && (
              <span>{t("member_notified").replace("{name}", data.name)}</span>
            )}
          </div>
        )}

        {/* Feedback: stale (refusal) */}
        {feedback?.type === "error" && (
          <div className="mb-6">
            <div
              role="alert"
              className="flex items-start gap-2 px-4 py-3 rounded-sm bg-coral-tint border border-coral/30 text-coral text-[14px] mb-4"
            >
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span>{feedback.message}</span>
            </div>
            {/* Fresh version (when stale) */}
            {feedback.freshRoutine && feedback.freshRoutine.length > 0 && (
              <details open className="mb-4">
                <summary className="text-[14px] text-ink-2 cursor-pointer hover:text-ink">
                  {t("current_version_label")}
                </summary>
                <div className="mt-2 space-y-2 pl-4 border-l-2 border-elevation-0-stroke">
                  {feedback.freshRoutine.map((day) => (
                    <div key={day.weekday}>
                      <span className="text-[14px] font-semibold text-ink">
                        {getWeekdays()[day.weekday]}
                      </span>{" "}
                      <span className="text-[14px] text-ink-2">{day.name}</span>
                      <ul className="mt-1 space-y-0.5">
                        {day.exercises.map((ex, i) => (
                          <li key={i} className="text-[13px] text-ink-2">
                            {ex.exercise}
                            {ex.sets != null && `, ${ex.sets}`}
                            {ex.reps && `, ${ex.reps}`}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}

        {/* Form */}
        <form
          ref={formRef}
          onSubmit={handleSubmit(onSubmit as any)}
          noValidate
        >
          <input type="hidden" {...register("base_routine_id", { valueAsNumber: true })} />

          {/* Validation error summary */}
          {Object.keys(errors).length > 0 && (
            <div className="mb-4 px-4 py-3 rounded-sm bg-coral-tint border border-coral/30 text-coral text-[14px]">
              {errors.workouts?.root?.message && (
                <p>{errors.workouts.root.message}</p>
              )}
              {errors.workouts?.message && (
                <p>{String(errors.workouts.message)}</p>
              )}
            </div>
          )}

          {/* Day blocks */}
          <div className="space-y-5" id="days">
            {fields.map((field, dayIndex) => {
              const dayErrors = errors.workouts?.[dayIndex];
              const day = workouts?.[dayIndex];
              return (
                <fieldset
                  key={field.id}
                  className="rounded-xl border border-elevation-0-stroke bg-elevation-1 p-4"
                >
                  <div className="flex items-start justify-between gap-3 mb-3">
                    <div className="flex-1 min-w-0">
                      {/* Weekday select */}
                      <select
                        {...register(`workouts.${dayIndex}.weekday`, {
                          valueAsNumber: true,
                        })}
                        className="w-full min-w-0 bg-elevation-0 border border-elevation-0-stroke rounded-sm px-3 py-2 text-[14px] text-ink focus:outline-none focus:border-ink-2 appearance-none"
                        style={{
                          backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%239e9ea8'%3E%3Cpath d='M3 5l3 3 3-3'/%3E%3C/svg%3E")`,
                          backgroundRepeat: "no-repeat",
                          backgroundPosition: "right 10px center",
                          paddingRight: "2rem",
                        }}
                      >
                        <option value="">{t("pick_day")}</option>
                        {(day
                          ? [day.weekday]
                          : []
                        ).concat(
                          Array.from({ length: 7 }, (_, i) => i).filter(
                            (w) => w !== day?.weekday
                          )
                        ).map((w) => (
                          <option key={w} value={w}>
                            {getWeekdays()[w]}
                          </option>
                        ))}
                      </select>
                      {dayErrors?.weekday && (
                        <p className="mt-1 text-[12px] text-[#fca5a5]">
                          {String(dayErrors.weekday.message)}
                        </p>
                      )}
                    </div>

                    {/* Remove day button */}
                    <button
                      type="button"
                      onClick={() => {
                        remove(dayIndex);
                        setFeedback(null);
                      }}
                      className="flex-shrink-0 p-1.5 rounded-sm text-ink-2 hover:text-[#f87171] hover:bg-elevation-2 motion-safe:transition-colors duration-fast"
                      aria-label={t("remove_day")}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>

                  {/* Workout name */}
                  <div className="mb-3">
                    <input
                      type="text"
                      {...register(`workouts.${dayIndex}.name`)}
                      placeholder={t("workout_name_placeholder")}
                      maxLength={100}
                      className="w-full bg-elevation-0 border border-elevation-0-stroke rounded-sm px-3 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2"
                    />
                    {dayErrors?.name && (
                      <p className="mt-1 text-[12px] text-[#fca5a5]">
                        {String(dayErrors.name.message)}
                      </p>
                    )}
                  </div>

                  {/* Exercises sub-form */}
                  <ExerciseList
                    dayIndex={dayIndex}
                    control={control}
                    register={register}
                    errors={errors}
                    catalog={data.catalog}
                  />
                </fieldset>
              );
            })}
          </div>

          {/* Add day button */}
          {canAddDay && (
            <button
              type="button"
              onClick={addDay}
              className="mt-4 flex items-center gap-1.5 text-[14px] text-ink-2 hover:text-ink motion-safe:transition-colors duration-fast"
            >
              <Plus className="w-4 h-4" />
              {t("add_day")}
            </button>
          )}

          {/* Catalog reference */}
          {data && data.catalog.length > 0 && (
            <details className="mt-6">
              <summary className="text-[14px] text-ink-2 cursor-pointer hover:text-ink flex items-center gap-1">
                <ChevronDown className="w-4 h-4" />
                {t("catalog_label")}
              </summary>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {data.catalog.map((name) => (
                  <button
                    type="button"
                    key={name}
                    className="inline-block text-[13px] px-2 py-0.5 rounded-full bg-elevation-1 border border-elevation-0-stroke text-ink-2 cursor-pointer hover:text-ink hover:border-ink-3 motion-safe:transition-colors duration-fast"
                    onClick={() => {
                      // Fill through React Hook Form rather than reaching into
                      // its generated DOM names. That keeps the form state,
                      // validation, and focus in one model.
                      for (let dayIndex = 0; dayIndex < workouts.length; dayIndex += 1) {
                        const exercises = workouts[dayIndex]?.exercises ?? [];
                        for (let exIndex = 0; exIndex < exercises.length; exIndex += 1) {
                          if (!exercises[exIndex].exercise.trim()) {
                            const path =
                              `workouts.${dayIndex}.exercises.${exIndex}.exercise` as const;
                            setValue(path, name, {
                              shouldDirty: true,
                              shouldValidate: true,
                            });
                            setFocus(path);
                            return;
                          }
                        }
                      }
                    }}
                  >
                    {name}
                  </button>
                ))}
              </div>
            </details>
          )}

          {/* Help text */}
          <p className="mt-4 text-[13px] text-ink-3">{t("editor_help")}</p>

          {/* Submit */}
          <div className="mt-6">
            <button
              type="submit"
              disabled={saveMutation.isPending}
              className="inline-flex items-center gap-2 px-6 py-2.5 rounded-sm bg-magenta text-ink text-[14px] font-medium hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed motion-safe:transition-all duration-fast"
            >
              {saveMutation.isPending && (
                <Loader2 className="w-4 h-4 motion-safe:animate-spin" />
              )}
              {t("save_routine")}
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}

// --- ExerciseList sub-component ---

function ExerciseList({
  dayIndex,
  control,
  register,
  errors,
  catalog,
}: {
  dayIndex: number;
  control: any;
  register: any;
  errors: any;
  catalog: string[];
}) {
  const t = useT();
  const { fields, append, remove } = useFieldArray({
    control,
    name: `workouts.${dayIndex}.exercises`,
  });

  return (
    <div className="space-y-2">
      {fields.map((field, exIndex) => {
        const exErrors =
          errors.workouts?.[dayIndex]?.exercises?.[exIndex];
        const exPath = `workouts.${dayIndex}.exercises.${exIndex}`;
        return (
          <div key={field.id} className="flex items-start gap-2">
            {/* Exercise name with catalog autocomplete */}
            <div className="flex-1 min-w-0 relative">
              <input
                type="text"
                {...register(`${exPath}.exercise`)}
                placeholder="squat"
                list={`catalog-list-${dayIndex}`}
                autoComplete="off"
                className="w-full bg-elevation-0 border border-elevation-0-stroke rounded-sm px-3 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2"
              />
              <datalist id={`catalog-list-${dayIndex}`}>
                {catalog.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              {exErrors?.exercise && (
                <p className="mt-0.5 text-[12px] text-[#fca5a5]">
                  {String(exErrors.exercise.message)}
                </p>
              )}
            </div>

            {/* Sets */}
            <div className="w-16 flex-shrink-0">
              <input
                type="number"
                {...register(`${exPath}.sets`, {
                  valueAsNumber: true,
                })}
                placeholder="sets"
                min={1}
                max={99}
                className="w-full bg-elevation-0 border border-elevation-0-stroke rounded-sm px-2 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 text-center"
              />
              {exErrors?.sets && (
                <p className="mt-0.5 text-[12px] text-[#fca5a5] text-center">
                  {String(exErrors.sets.message)}
                </p>
              )}
            </div>

            {/* Reps */}
            <div className="w-20 flex-shrink-0">
              <input
                type="text"
                {...register(`${exPath}.reps`)}
                placeholder="8-10"
                maxLength={40}
                className="w-full bg-elevation-0 border border-elevation-0-stroke rounded-sm px-2 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 text-center"
              />
              {exErrors?.reps && (
                <p className="mt-0.5 text-[12px] text-[#fca5a5] text-center">
                  {String(exErrors.reps.message)}
                </p>
              )}
            </div>

            {/* Remove exercise */}
            <button
              type="button"
              onClick={() => {
                if (fields.length > 1) {
                  remove(exIndex);
                }
              }}
              disabled={fields.length <= 1}
              className="flex-shrink-0 p-1.5 mt-0.5 rounded-sm text-ink-2 hover:text-[#f87171] hover:bg-elevation-2 disabled:opacity-30 disabled:cursor-not-allowed motion-safe:transition-colors duration-fast"
              aria-label={t("remove_exercise")}
            >
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        );
      })}

      {/* Add exercise button */}
      <button
        type="button"
        onClick={() => append(defaultExercise())}
        className="flex items-center gap-1 text-[13px] text-ink-2 hover:text-ink motion-safe:transition-colors duration-fast mt-1"
      >
        <Plus className="w-3.5 h-3.5" />
        {t("add_exercise")}
      </button>
    </div>
  );
}
