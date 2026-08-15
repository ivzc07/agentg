import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Building2Icon,
  CheckIcon,
  CopyIcon,
  ShieldCheckIcon,
  UsersRoundIcon,
} from "lucide-react";
import { toast } from "sonner";
import {
  fetchSettings,
  regenerateCoach,
  regenerateInvite,
  renameGym,
} from "../api/settings";
import { useT } from "../hooks/useT";
import { AppHeader } from "./AppHeader";
import { Alert, AlertDescription } from "./ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "./ui/alert-dialog";
import { Button } from "./ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "./ui/card";
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "./ui/field";
import { Input } from "./ui/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "./ui/input-group";
import { Spinner } from "./ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import { Toaster } from "./ui/sonner";

interface LinkCardProps {
  description: string;
  id: string;
  onCopy: () => void;
  regenerate: {
    confirm: string;
    error: Error | null;
    isPending: boolean;
    onChange: (value: string) => void;
    onConfirm: () => Promise<unknown>;
    onReset: () => void;
    warning: string;
  };
  title: string;
  url: string;
  copied: boolean;
  qrSvg?: string;
}

function plainConfirmationPrompt(template: string, word: string): string {
  return template.replace(/<\/?b>/g, "").replace("{word}", word);
}

function RegenerateDialog({
  confirm,
  error,
  isPending,
  onChange,
  onConfirm,
  onReset,
  title,
  warning,
}: LinkCardProps["regenerate"] & { title: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const confirmWord = t("confirm_word");
  const matches = confirm.trim().toLowerCase() === confirmWord.toLowerCase();
  const inputId = `confirm-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;

  const setDialogOpen = (next: boolean) => {
    setOpen(next);
    if (!next) {
      onReset();
    }
  };

  const submit = async () => {
    try {
      await onConfirm();
      setOpen(false);
    } catch {
      // The mutation error is rendered next to the confirmation field.
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={setDialogOpen}>
      <AlertDialogTrigger
        render={
          <Button variant="destructive" type="button">
            {t("regenerate")}
          </Button>
        }
      />
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {t("regenerate")}: {title.toLowerCase()}
          </AlertDialogTitle>
          <AlertDialogDescription>{warning}</AlertDialogDescription>
        </AlertDialogHeader>

        <FieldGroup>
          <Field data-invalid={Boolean(error)}>
            <FieldLabel htmlFor={inputId}>
              {plainConfirmationPrompt(t("confirm_prompt"), confirmWord)}
            </FieldLabel>
            <Input
              id={inputId}
              value={confirm}
              onChange={(event) => onChange(event.target.value)}
              autoComplete="off"
              placeholder={confirmWord}
              aria-invalid={Boolean(error)}
            />
            <FieldError>{error?.message}</FieldError>
          </Field>
        </FieldGroup>

        <AlertDialogFooter>
          <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            onClick={submit}
            disabled={!matches || isPending}
          >
            {isPending ? <Spinner data-icon="inline-start" /> : null}
            {t("regenerate")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function LinkField({
  copied,
  id,
  onCopy,
  title,
  url,
}: Pick<LinkCardProps, "copied" | "id" | "onCopy" | "title" | "url">) {
  const t = useT();

  return (
    <Field>
      <FieldLabel htmlFor={`${id}-url`} className="sr-only">
        {title}
      </FieldLabel>
      <InputGroup className="h-12 rounded-lg border-border bg-background px-1 shadow-none">
        <InputGroupInput
          id={`${id}-url`}
          value={url}
          readOnly
          aria-label={title}
          className="font-mono text-[13px] font-medium text-foreground"
        />
        <InputGroupAddon align="inline-end">
          <InputGroupButton
            size="sm"
            variant={copied ? "secondary" : "default"}
            className="h-9 px-3"
            onClick={onCopy}
            aria-label={copied ? t("copied") : t("copy")}
            aria-live="polite"
          >
            {copied ? <CheckIcon data-icon="inline-start" /> : <CopyIcon data-icon="inline-start" />}
            {copied ? t("copied") : t("copy")}
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
    </Field>
  );
}

function LinkCard({
  copied,
  description,
  id,
  onCopy,
  qrSvg,
  regenerate,
  title,
  url,
}: LinkCardProps) {
  if (qrSvg) {
    return (
      <Card id={id} className="gap-0 overflow-hidden rounded-xl py-0 ring-1 ring-foreground/15">
        <div className="grid lg:grid-cols-[minmax(0,1fr)_18rem]">
          <div className="flex min-w-0 flex-col">
            <CardHeader className="gap-3 px-6 pt-6 pb-5 md:px-8 md:pt-8">
              <div className="flex items-center gap-3">
                <UsersRoundIcon className="size-6 text-foreground" aria-hidden="true" />
                <CardTitle className="text-[20px] font-semibold tracking-[-0.02em]">
                  <h2>{title}</h2>
                </CardTitle>
              </div>
              <CardDescription className="max-w-[52ch] text-[15px] leading-6">
                {description}
              </CardDescription>
            </CardHeader>

            <CardContent className="px-6 pb-7 md:px-8">
              <LinkField
                copied={copied}
                id={id}
                onCopy={onCopy}
                title={title}
                url={url}
              />
            </CardContent>

            <CardFooter className="mt-auto justify-end px-6 py-4 md:px-8">
              <RegenerateDialog title={title} {...regenerate} />
            </CardFooter>
          </div>

          <div className="flex min-h-72 items-center justify-center border-t border-black/10 bg-foreground p-8 text-background lg:border-t-0 lg:border-l">
            <div
              className="size-52 max-w-full rounded-lg bg-white p-3 ring-1 ring-black/10 [&>svg]:size-full"
              aria-label={title}
              dangerouslySetInnerHTML={{ __html: qrSvg }}
            />
          </div>
        </div>
      </Card>
    );
  }

  return (
    <Card id={id} className="gap-0 rounded-xl bg-primary py-0 text-primary-foreground ring-1 ring-foreground/30">
      <div className="grid md:grid-cols-[minmax(14rem,0.68fr)_minmax(0,1fr)]">
        <CardHeader className="gap-3 border-b border-foreground/20 px-6 py-6 md:border-r md:border-b-0 md:px-7">
          <div className="flex items-center gap-3">
            <ShieldCheckIcon className="size-6" aria-hidden="true" />
            <CardTitle className="text-[20px] font-semibold tracking-[-0.02em]">
              <h2>{title}</h2>
            </CardTitle>
          </div>
          <CardDescription className="max-w-[34ch] leading-5 text-primary-foreground/70">
            {description}
          </CardDescription>
        </CardHeader>

        <div className="flex min-w-0 flex-col">
          <CardContent className="px-6 py-6 md:px-7">
            <LinkField
              copied={copied}
              id={id}
              onCopy={onCopy}
              title={title}
              url={url}
            />
          </CardContent>
          <CardFooter className="mt-auto justify-end bg-foreground px-6 py-4 md:px-7">
            <RegenerateDialog title={title} {...regenerate} />
          </CardFooter>
        </div>
      </div>
    </Card>
  );
}

export function SettingsPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [inviteConfirm, setInviteConfirm] = useState("");
  const [coachConfirm, setCoachConfirm] = useState("");
  const [gymName, setGymName] = useState("");
  const [gymNameDirty, setGymNameDirty] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (data && !gymNameDirty) {
      setGymName(data.gym_name);
    }
  }, [data, gymNameDirty]);

  const regenerateInviteMutation = useMutation({
    mutationFn: () => regenerateInvite(inviteConfirm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setInviteConfirm("");
      toast.success(t("done_link_regenerated"));
    },
  });

  const regenerateCoachMutation = useMutation({
    mutationFn: () => regenerateCoach(coachConfirm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      setCoachConfirm("");
      toast.success(t("done_link_regenerated"));
    },
  });

  const renameGymMutation = useMutation({
    mutationFn: () => renameGym(gymName),
    onSuccess: (response) => {
      setGymName(response.gym_name);
      setGymNameDirty(false);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      toast.success(t("done_saved"));
    },
  });

  const copyUrl = async (text: string, key: string) => {
    if (!navigator.clipboard) {
      toast.error(t("copy_failed"));
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey(null), 2000);
    } catch {
      toast.error(t("copy_failed"));
    }
  };

  if (isLoading) {
    return (
      <div className="flex min-h-[200px] items-center justify-center text-muted-foreground" aria-busy="true">
        <Spinner />
        <span className="sr">Loading…</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="mx-auto max-w-xl px-gut py-8">
        <Alert variant="destructive">
          <AlertDescription>{t("settings_load_error")}</AlertDescription>
        </Alert>
      </div>
    );
  }

  const inviteError = regenerateInviteMutation.error instanceof Error
    ? regenerateInviteMutation.error
    : null;
  const coachError = regenerateCoachMutation.error instanceof Error
    ? regenerateCoachMutation.error
    : null;
  const gymError = renameGymMutation.error instanceof Error
    ? renameGymMutation.error
    : null;

  return (
    <>
      <AppHeader gym={data.gym_name} variant="settings-brand">
        <main className="mx-auto w-full max-w-[1440px] px-4 py-5 sm:px-6 lg:px-8 lg:py-8">
          <Tabs defaultValue="access" className="gap-0">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
              <h1 className="text-[27px] font-semibold tracking-[-0.03em]">
                {t("settings_title")}
              </h1>
              <TabsList
                aria-label={t("settings_title")}
                className="w-full border border-elevation-0-stroke bg-white p-1 shadow-shadow-1 group-data-horizontal/tabs:h-10 sm:w-auto"
              >
                <TabsTrigger value="access" className="px-6">
                  {t("settings_tab_access")}
                </TabsTrigger>
                <TabsTrigger value="general" className="px-6">
                  {t("settings_tab_general")}
                </TabsTrigger>
              </TabsList>
            </div>

            <TabsContent value="access" className="flex flex-col gap-5 pt-6">
              <LinkCard
                id="invite"
                title={t("invite_section")}
                description={`${t("invite_blurb")} ${data.gym_name}.`}
                url={data.invite_url}
                qrSvg={data.qr_svg}
                copied={copiedKey === "invite"}
                onCopy={() => copyUrl(data.invite_url, "invite")}
                regenerate={{
                  confirm: inviteConfirm,
                  error: inviteError,
                  isPending: regenerateInviteMutation.isPending,
                  onChange: (value) => {
                    regenerateInviteMutation.reset();
                    setInviteConfirm(value);
                  },
                  onConfirm: () => regenerateInviteMutation.mutateAsync(),
                  onReset: () => {
                    regenerateInviteMutation.reset();
                    setInviteConfirm("");
                  },
                  warning: t("invite_warning"),
                }}
              />

              <LinkCard
                id="coach-link"
                title={t("coach_section")}
                description={t("coach_blurb")}
                url={data.coach_invite_url}
                copied={copiedKey === "coach"}
                onCopy={() => copyUrl(data.coach_invite_url, "coach")}
                regenerate={{
                  confirm: coachConfirm,
                  error: coachError,
                  isPending: regenerateCoachMutation.isPending,
                  onChange: (value) => {
                    regenerateCoachMutation.reset();
                    setCoachConfirm(value);
                  },
                  onConfirm: () => regenerateCoachMutation.mutateAsync(),
                  onReset: () => {
                    regenerateCoachMutation.reset();
                    setCoachConfirm("");
                  },
                  warning: t("coach_warning"),
                }}
              />
            </TabsContent>

            <TabsContent value="general" className="pt-6">
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  renameGymMutation.mutate();
                }}
              >
                <Card className="gap-0 overflow-hidden rounded-xl py-0 ring-1 ring-foreground/15">
                  <div className="grid min-h-80 lg:grid-cols-[0.8fr_1.2fr]">
                    <CardHeader className="content-start gap-5 bg-foreground px-6 py-8 text-background md:px-8 md:py-10">
                      <Building2Icon className="size-8 text-lime" aria-hidden="true" />
                      <div className="space-y-2">
                        <CardTitle className="text-[22px] font-semibold tracking-[-0.02em]">
                          <h2>{t("gym_name_section")}</h2>
                        </CardTitle>
                        <CardDescription className="max-w-[34ch] text-[15px] leading-6 text-background/70">
                          {t("gym_name_help")}
                        </CardDescription>
                      </div>
                    </CardHeader>

                    <div className="flex min-w-0 flex-col">
                      <CardContent className="flex-1 px-6 py-8 md:px-8 md:py-10">
                        <FieldGroup>
                          <Field data-invalid={Boolean(gymError)}>
                            <FieldLabel htmlFor="gym-name" className="text-sm font-semibold">
                              {t("gym_name_section")}
                            </FieldLabel>
                            <Input
                              id="gym-name"
                              name="name"
                              value={gymName}
                              className="h-12 bg-background px-4 text-base"
                              onChange={(event) => {
                                renameGymMutation.reset();
                                setGymNameDirty(true);
                                setGymName(event.target.value);
                              }}
                              maxLength={200}
                              aria-invalid={Boolean(gymError)}
                            />
                            <FieldError>{gymError?.message}</FieldError>
                          </Field>
                        </FieldGroup>
                      </CardContent>
                      <CardFooter className="justify-end px-6 py-4 md:px-8">
                        <Button
                          type="submit"
                          size="lg"
                          disabled={!gymName.trim() || renameGymMutation.isPending}
                        >
                          {renameGymMutation.isPending ? <Spinner data-icon="inline-start" /> : null}
                          {t("save")}
                        </Button>
                      </CardFooter>
                    </div>
                  </div>
                </Card>
              </form>
            </TabsContent>
          </Tabs>
        </main>
      </AppHeader>
      <Toaster />
    </>
  );
}
