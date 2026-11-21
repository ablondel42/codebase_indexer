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

const ALL_MODELS = PROMPT_MODEL_PROVIDERS.flatMap(
  (provider) => provider.models,
);

export function SessionPromptInput({
  draft,
  onDraftChange,
  hasActiveSession,
  promptEnabled = false,
  includeCbiMcp,
  onToggleCbiMcp,
  onSubmitPrompt,
}: {
  draft: string;
  onDraftChange: (value: string) => void;
  hasActiveSession: boolean;
  promptEnabled?: boolean;
  includeCbiMcp: boolean;
  onToggleCbiMcp: (enabled: boolean) => void;
  onSubmitPrompt?: (payload: {
    text: string;
    model: string;
    effort: PromptEffortId;
    files: File[];
  }) => Promise<void> | void;
}) {
  const [files, setFiles] = React.useState<AttachedDraftFile[]>([]);
  const [model, setModel] = React.useState<string>(
    ALL_MODELS[0]?.id ?? "gemini-3.1-pro",
  );
  const [effort, setEffort] = React.useState<PromptEffortId>("medium");
  const formRef = React.useRef<HTMLFormElement>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const currentModel =
    ALL_MODELS.find((item) => item.id === model) ?? ALL_MODELS[0]!;
  const currentEffort =
    PROMPT_EFFORT_OPTIONS.find((item) => item.id === effort) ??
    PROMPT_EFFORT_OPTIONS[1];

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
      model,
      effort,
      files: files.map((f) => f.file),
    });
    onDraftChange("");
    setFiles([]);
  }

  const placeholder = !hasActiveSession
    ? "Create or load a session to prepare a prompt…"
    : promptEnabled
      ? "Ask the agent to inspect or refactor code…"
      : "Draft a prompt (live session/prompt dispatch coming next)…";

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
          value={draft}
          disabled={!hasActiveSession}
          onChange={(event) => onDraftChange(event.target.value)}
          placeholder={placeholder}
          aria-label="Prompt message"
          className="min-h-14 p-2.5 text-xs"
          onKeyDown={(event) => {
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

          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <InputGroupButton
                  size="xs"
                  variant="outline"
                  aria-label={`Model: ${currentModel.name}`}
                  className="ml-auto"
                  disabled={!hasActiveSession}
                />
              }
            >
              <span className="truncate">{currentModel.name}</span>
              <ChevronDownIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="w-48">
              <DropdownMenuRadioGroup
                value={model}
                onValueChange={(next) => setModel(String(next))}
              >
                {PROMPT_MODEL_PROVIDERS.map((provider, index) => (
                  <React.Fragment key={provider.name}>
                    {index > 0 && <DropdownMenuSeparator />}
                    <DropdownMenuGroup>
                      <DropdownMenuLabel>{provider.name}</DropdownMenuLabel>
                      {provider.models.map((item) => (
                        <DropdownMenuRadioItem
                          key={item.id}
                          value={item.id}
                          closeOnClick
                        >
                          {item.name}
                        </DropdownMenuRadioItem>
                      ))}
                    </DropdownMenuGroup>
                  </React.Fragment>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>

          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <InputGroupButton
                  size="xs"
                  variant="outline"
                  aria-label={`Effort: ${currentEffort.name}`}
                  disabled={!hasActiveSession}
                />
              }
            >
              <span className="text-muted-foreground">Effort</span>
              <span>{currentEffort.name}</span>
              <ChevronDownIcon />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="w-36">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Reasoning Effort</DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={effort}
                  onValueChange={(next) => setEffort(next as PromptEffortId)}
                >
                  {PROMPT_EFFORT_OPTIONS.map((item) => (
                    <DropdownMenuRadioItem
                      key={item.id}
                      value={item.id}
                      closeOnClick
                    >
                      {item.name}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>

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
                  : "Live session/prompt dispatch is not enabled on the bridge yet"}
              </p>
            </TooltipContent>
          </Tooltip>
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
