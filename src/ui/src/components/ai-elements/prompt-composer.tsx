import * as React from "react";
import {
  ArrowUpIcon,
  ChevronDownIcon,
  DatabaseIcon,
  FileTextIcon,
  PaperclipIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";

import {
  Attachment,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentMedia,
  AttachmentTitle,
} from "@/components/ui/attachment";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  PROMPT_EFFORT_OPTIONS,
  PROMPT_MODEL_PROVIDERS,
  type PromptEffortId,
} from "@/lib/acp/turn-grouping";
import type {
  AcpAvailableCommand,
  AcpConfigOption,
  AcpConfigSelectChoice,
  AcpModelsState,
  AcpModesState,
} from "@/lib/acp/types";

interface AttachedDraftFile {
  id: string;
  name: string;
  file: File;
}

export function PromptSuggestions({
  suggestions,
  onSelect,
  disabled = false,
}: {
  suggestions: string[];
  onSelect: (suggestion: string) => void;
  disabled?: boolean;
}) {
  if (suggestions.length === 0) {
    return null;
  }

  return (
    <div className="flex scroll-fade-x scrollbar-none gap-1.5 overflow-x-auto overscroll-x-contain py-0.5">
      {suggestions.map((suggestion) => (
        <Button
          key={suggestion}
          variant="outline"
          size="xs"
          className="shrink-0"
          disabled={disabled}
          onClick={() => onSelect(suggestion)}
        >
          {suggestion}
        </Button>
      ))}
    </div>
  );
}

const FALLBACK_MODELS = PROMPT_MODEL_PROVIDERS.flatMap(
  (provider) => provider.models,
);

function flattenConfigChoices(opt: AcpConfigOption): AcpConfigSelectChoice[] {
  if (opt.groups.length > 0) {
    return opt.groups.flatMap((grp) => grp.options);
  }
  return opt.options;
}

function extractSlashQuery(draft: string): string | null {
  if (!draft.startsWith("/")) {
    return null;
  }
  const firstSegment = draft.slice(1);
  if (/\s/.test(firstSegment)) {
    return null;
  }
  return firstSegment.toLowerCase();
}

export interface SessionPromptInputProps {
  draft: string;
  onDraftChange: (value: string) => void;
  hasActiveSession: boolean;
  promptEnabled?: boolean;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  configOptions?: AcpConfigOption[];
  sessionModels?: AcpModelsState | null;
  sessionModes?: AcpModesState | null;
  availableCommands?: AcpAvailableCommand[];
  configBusyId?: string | null;
  onSelectConfigOption?: (
    configId: string,
    value: string | boolean,
  ) => Promise<void> | void;
  onSelectMode?: (modeId: string) => Promise<void> | void;
  onSubmitPrompt?: (payload: {
    text: string;
    model: string;
    effort: string;
    files: File[];
  }) => Promise<void> | void;
}

export function SessionPromptInput({
  draft,
  onDraftChange,
  hasActiveSession,
  promptEnabled = false,
  includeCbiMcp,
  onToggleCbiMcp,
  configOptions = [],
  sessionModels = null,
  sessionModes = null,
  availableCommands = [],
  configBusyId = null,
  onSelectConfigOption,
  onSelectMode,
  onSubmitPrompt,
}: SessionPromptInputProps) {
  const [files, setFiles] = React.useState<AttachedDraftFile[]>([]);
  const [fallbackModel, setFallbackModel] = React.useState<string>(
    FALLBACK_MODELS[0]?.id ?? "gemini-3.1-pro",
  );
  const [fallbackEffort, setFallbackEffort] =
    React.useState<PromptEffortId>("medium");
  const [dismissedSlashDraft, setDismissedSlashDraft] = React.useState<
    string | null
  >(null);
  const [activeSlashIdx, setActiveSlashIdx] = React.useState(0);

  const formRef = React.useRef<HTMLFormElement>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  const modelConfigOpt = React.useMemo(
    () =>
      configOptions.find(
        (opt) =>
          (opt.category === "model" || opt.id === "model") &&
          opt.type === "select",
      ) ?? null,
    [configOptions],
  );
  const thoughtConfigOpt = React.useMemo(
    () =>
      configOptions.find(
        (opt) =>
          (opt.category === "thought_level" || opt.id === "thought_level") &&
          opt.type === "select",
      ) ?? null,
    [configOptions],
  );
  const modeConfigOpt = React.useMemo(
    () =>
      configOptions.find(
        (opt) =>
          (opt.category === "mode" || opt.id === "mode") &&
          opt.type === "select",
      ) ?? null,
    [configOptions],
  );

  const activeModelChoices: AcpConfigSelectChoice[] = React.useMemo(() => {
    if (modelConfigOpt) {
      return flattenConfigChoices(modelConfigOpt);
    }
    if (sessionModels && sessionModels.availableModels.length > 0) {
      return sessionModels.availableModels.map((item) => ({
        value: item.modelId,
        name: item.name,
        description: item.description,
      }));
    }
    return FALLBACK_MODELS.map((item) => ({ value: item.id, name: item.name }));
  }, [modelConfigOpt, sessionModels]);

  const activeModelValue =
    modelConfigOpt && typeof modelConfigOpt.currentValue === "string"
      ? modelConfigOpt.currentValue
      : (sessionModels?.currentModelId ?? fallbackModel);

  const activeModelLabel =
    activeModelChoices.find((item) => item.value === activeModelValue)?.name ??
    activeModelValue;

  const activeThoughtChoices: AcpConfigSelectChoice[] = React.useMemo(() => {
    if (thoughtConfigOpt) {
      return flattenConfigChoices(thoughtConfigOpt);
    }
    if (configOptions.length > 0) {
      return [];
    }
    return PROMPT_EFFORT_OPTIONS.map((item) => ({
      value: item.id,
      name: item.name,
    }));
  }, [configOptions.length, thoughtConfigOpt]);

  const activeThoughtValue =
    thoughtConfigOpt && typeof thoughtConfigOpt.currentValue === "string"
      ? thoughtConfigOpt.currentValue
      : fallbackEffort;

  const activeThoughtLabel =
    activeThoughtChoices.find((item) => item.value === activeThoughtValue)
      ?.name ?? activeThoughtValue;

  const activeModeChoices: AcpConfigSelectChoice[] = React.useMemo(() => {
    if (modeConfigOpt) {
      return flattenConfigChoices(modeConfigOpt);
    }
    if (sessionModes && sessionModes.availableModes.length > 0) {
      return sessionModes.availableModes.map((item) => ({
        value: item.id,
        name: item.name,
        description: item.description,
      }));
    }
    return [];
  }, [modeConfigOpt, sessionModes]);

  const activeModeValue =
    modeConfigOpt && typeof modeConfigOpt.currentValue === "string"
      ? modeConfigOpt.currentValue
      : (sessionModes?.currentModeId ?? "");

  const activeModeLabel =
    activeModeChoices.find((item) => item.value === activeModeValue)?.name ??
    activeModeValue;

  const slashQuery = extractSlashQuery(draft);
  const matchingCommands = React.useMemo(() => {
    if (slashQuery === null || availableCommands.length === 0) {
      return [];
    }
    if (!slashQuery) {
      return availableCommands;
    }
    return availableCommands.filter(
      (cmd) =>
        cmd.name.toLowerCase().includes(slashQuery) ||
        cmd.description.toLowerCase().includes(slashQuery),
    );
  }, [availableCommands, slashQuery]);

  const showInlineSlashMenu =
    hasActiveSession &&
    slashQuery !== null &&
    dismissedSlashDraft !== draft &&
    matchingCommands.length > 0;

  const clampedSlashIdx =
    matchingCommands.length > 0
      ? Math.min(activeSlashIdx, matchingCommands.length - 1)
      : 0;

  function applySlashCommand(commandName: string) {
    const nextText = `/${commandName} `;
    onDraftChange(nextText);
    setDismissedSlashDraft(null);
    setActiveSlashIdx(0);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  }

  function handleModelChange(nextValue: string) {
    if (modelConfigOpt && onSelectConfigOption) {
      void onSelectConfigOption(modelConfigOpt.id, nextValue);
      return;
    }
    setFallbackModel(nextValue);
  }

  function handleThoughtChange(nextValue: string) {
    if (thoughtConfigOpt && onSelectConfigOption) {
      void onSelectConfigOption(thoughtConfigOpt.id, nextValue);
      return;
    }
    setFallbackEffort(nextValue as PromptEffortId);
  }

  function handleModeChange(nextValue: string) {
    if (modeConfigOpt && onSelectConfigOption) {
      void onSelectConfigOption(modeConfigOpt.id, nextValue);
      return;
    }
    if (onSelectMode) {
      void onSelectMode(nextValue);
    }
  }

  const canSend =
    promptEnabled && hasActiveSession && draft.trim().length > 0;

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files;
    if (!selected) {
      return;
    }
    setFiles((prev) => [
      ...prev,
      ...Array.from(selected, (file) => ({
        id: crypto.randomUUID(),
        name: file.name,
        file,
      })),
    ]);
    event.target.value = "";
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend || !onSubmitPrompt) {
      return;
    }
    void onSubmitPrompt({
      text: draft.trim(),
      model: activeModelValue,
      effort: activeThoughtValue,
      files: files.map((f) => f.file),
    });
    onDraftChange("");
    setFiles([]);
  }

  const placeholder = !hasActiveSession
    ? "Create or resume a session to prepare a prompt…"
    : promptEnabled
      ? "Ask the agent or type / for commands…"
      : "Type / to browse session commands (live prompt dispatch coming next)…";

  return (
    <form ref={formRef} onSubmit={handleSubmit} className="w-full">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="sr-only"
        tabIndex={-1}
        onChange={handleFileChange}
      />
      <InputGroup>
        {showInlineSlashMenu && (
          <InputGroupAddon
            align="block-start"
            className="w-full flex-col items-stretch gap-1 border-b border-border/60 pb-2"
          >
            <div className="flex items-center justify-between px-1 text-[11px] text-muted-foreground">
              <span>Slash commands ({matchingCommands.length})</span>
              <span>↑↓ navigate · Tab/Enter insert · Esc close</span>
            </div>
            <ScrollArea className="max-h-44 w-full">
              <div className="flex flex-col gap-0.5 pr-2">
                {matchingCommands.map((cmd, idx) => {
                  const isHighlighted = idx === clampedSlashIdx;
                  return (
                    <button
                      key={cmd.name}
                      type="button"
                      onMouseEnter={() => setActiveSlashIdx(idx)}
                      onClick={() => applySlashCommand(cmd.name)}
                      className={`flex w-full flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${
                        isHighlighted
                          ? "bg-accent text-accent-foreground"
                          : "hover:bg-muted/60"
                      }`}
                    >
                      <div className="flex w-full items-center justify-between gap-2">
                        <span className="font-mono font-medium">
                          /{cmd.name}
                        </span>
                        {cmd.inputHint && (
                          <Badge variant="outline" className="text-[10px]">
                            {cmd.inputHint}
                          </Badge>
                        )}
                      </div>
                      {cmd.description && (
                        <span className="line-clamp-2 text-[11px] text-muted-foreground">
                          {cmd.description}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </ScrollArea>
          </InputGroupAddon>
        )}

        {files.length > 0 && (
          <InputGroupAddon align="block-start" className="flex-wrap">
            {files.map((item) => (
              <Attachment key={item.id} size="xs">
                <AttachmentMedia>
                  <FileTextIcon />
                </AttachmentMedia>
                <AttachmentContent>
                  <AttachmentTitle>{item.name}</AttachmentTitle>
                </AttachmentContent>
                <AttachmentActions>
                  <AttachmentAction
                    aria-label={`Remove ${item.name}`}
                    onClick={() =>
                      setFiles((prev) =>
                        prev.filter((candidate) => candidate.id !== item.id),
                      )
                    }
                  >
                    <XIcon />
                  </AttachmentAction>
                </AttachmentActions>
              </Attachment>
            ))}
          </InputGroupAddon>
        )}

        <InputGroupTextarea
          ref={textareaRef}
          value={draft}
          disabled={!hasActiveSession}
          onChange={(event) => {
            onDraftChange(event.target.value);
            setDismissedSlashDraft(null);
            setActiveSlashIdx(0);
          }}
          placeholder={placeholder}
          aria-label="Prompt message"
          className="min-h-14 p-2.5 text-xs"
          onKeyDown={(event) => {
            if (showInlineSlashMenu) {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveSlashIdx(
                  (prev) => (prev + 1) % matchingCommands.length,
                );
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveSlashIdx(
                  (prev) =>
                    (prev - 1 + matchingCommands.length) %
                    matchingCommands.length,
                );
                return;
              }
              if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
                const chosen = matchingCommands[clampedSlashIdx];
                if (chosen) {
                  event.preventDefault();
                  applySlashCommand(chosen.name);
                  return;
                }
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setDismissedSlashDraft(draft);
                return;
              }
            }

            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              formRef.current?.requestSubmit();
            }
          }}
        />

        <InputGroupAddon align="block-end" className="flex-wrap gap-1.5 pt-1">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <InputGroupButton
                  aria-label="Add context"
                  type="button"
                  size="icon-xs"
                  variant="outline"
                  className="rounded-full"
                  disabled={!hasActiveSession}
                />
              }
            >
              <PlusIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" side="top" className="w-52">
              <DropdownMenuItem onClick={() => fileInputRef.current?.click()}>
                <PaperclipIcon />
                Attach files
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuCheckboxItem
                checked={includeCbiMcp}
                onCheckedChange={(checked) => onToggleCbiMcp(Boolean(checked))}
              >
                <DatabaseIcon />
                Attach CBI MCP index
              </DropdownMenuCheckboxItem>
            </DropdownMenuContent>
          </DropdownMenu>

          {activeModeChoices.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <InputGroupButton
                    size="xs"
                    variant="outline"
                    aria-label={`Mode: ${activeModeLabel}`}
                    disabled={
                      !hasActiveSession ||
                      configBusyId === (modeConfigOpt?.id ?? "mode")
                    }
                  />
                }
              >
                {configBusyId === (modeConfigOpt?.id ?? "mode") ? (
                  <Spinner className="size-3" />
                ) : null}
                <span className="text-muted-foreground">Mode</span>
                <span className="truncate">{activeModeLabel}</span>
                <ChevronDownIcon />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top" className="w-60">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>
                    {modeConfigOpt?.name ?? "Session Mode"}
                  </DropdownMenuLabel>
                  <DropdownMenuRadioGroup
                    value={activeModeValue}
                    onValueChange={(next) => handleModeChange(String(next))}
                  >
                    {activeModeChoices.map((item) => (
                      <DropdownMenuRadioItem
                        key={item.value}
                        value={item.value}
                        closeOnClick
                        className="flex flex-col items-start gap-0.5"
                      >
                        <span>{item.name}</span>
                        {item.description && (
                          <span className="text-[11px] text-muted-foreground">
                            {item.description}
                          </span>
                        )}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}

          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <InputGroupButton
                  size="xs"
                  variant="outline"
                  aria-label={`Model: ${activeModelLabel}`}
                  className="ml-auto"
                  disabled={
                    !hasActiveSession ||
                    (modelConfigOpt !== null && configBusyId === modelConfigOpt.id)
                  }
                />
              }
            >
              {modelConfigOpt && configBusyId === modelConfigOpt.id ? (
                <Spinner className="size-3" />
              ) : null}
              <span className="max-w-36 truncate">{activeModelLabel}</span>
              <ChevronDownIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              side="top"
              className="max-h-72 w-56 overflow-y-auto"
            >
              <DropdownMenuRadioGroup
                value={activeModelValue}
                onValueChange={(next) => handleModelChange(String(next))}
              >
                {modelConfigOpt && modelConfigOpt.groups.length > 0 ? (
                  modelConfigOpt.groups.map((grp, idx) => (
                    <React.Fragment key={grp.group}>
                      {idx > 0 && <DropdownMenuSeparator />}
                      <DropdownMenuGroup>
                        <DropdownMenuLabel>{grp.name}</DropdownMenuLabel>
                        {grp.options.map((item) => (
                          <DropdownMenuRadioItem
                            key={item.value}
                            value={item.value}
                            closeOnClick
                          >
                            {item.name}
                          </DropdownMenuRadioItem>
                        ))}
                      </DropdownMenuGroup>
                    </React.Fragment>
                  ))
                ) : (
                  <DropdownMenuGroup>
                    <DropdownMenuLabel>
                      {modelConfigOpt?.name ?? "Model"}
                    </DropdownMenuLabel>
                    {activeModelChoices.map((item) => (
                      <DropdownMenuRadioItem
                        key={item.value}
                        value={item.value}
                        closeOnClick
                      >
                        {item.name}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuGroup>
                )}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>

          {activeThoughtChoices.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <InputGroupButton
                    size="xs"
                    variant="outline"
                    aria-label={`Thinking: ${activeThoughtLabel}`}
                    disabled={
                      !hasActiveSession ||
                      (thoughtConfigOpt !== null &&
                        configBusyId === thoughtConfigOpt.id)
                    }
                  />
                }
              >
                {thoughtConfigOpt && configBusyId === thoughtConfigOpt.id ? (
                  <Spinner className="size-3" />
                ) : null}
                <span className="text-muted-foreground">
                  {thoughtConfigOpt ? "Thinking" : "Effort"}
                </span>
                <span>{activeThoughtLabel}</span>
                <ChevronDownIcon />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" side="top" className="w-40">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>
                    {thoughtConfigOpt?.name ?? "Reasoning Effort"}
                  </DropdownMenuLabel>
                  <DropdownMenuRadioGroup
                    value={activeThoughtValue}
                    onValueChange={(next) => handleThoughtChange(String(next))}
                  >
                    {activeThoughtChoices.map((item) => (
                      <DropdownMenuRadioItem
                        key={item.value}
                        value={item.value}
                        closeOnClick
                      >
                        {item.name}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}

          <Tooltip>
            <TooltipTrigger
              render={
                <span className="inline-flex">
                  <InputGroupButton
                    type="submit"
                    size="icon-xs"
                    variant="default"
                    className="rounded-full"
                    disabled={!canSend}
                    aria-label="Send prompt"
                  >
                    <ArrowUpIcon />
                  </InputGroupButton>
                </span>
              }
            />
            <TooltipContent side="top">
              <p>
                {promptEnabled
                  ? "Send prompt (Enter)"
                  : "Live session/prompt dispatch UI coming next"}
              </p>
            </TooltipContent>
          </Tooltip>
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
