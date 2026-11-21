import * as React from "react";
import {
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleIcon,
  ListChecksIcon,
  ListTodoIcon,
  LoaderCircleIcon,
  RouteIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { CodeBlock, CodeBlockContent } from "@/components/ui/code-block";
import {
  Trace,
  TraceCollapsible,
  TraceCollapsibleContent,
  TraceCollapsibleTrigger,
  TraceDescription,
  TraceIcon,
  TraceLine,
  TraceTitle,
} from "@/components/ui/trace";
import type {
  AcpPlanStep,
  AcpSubagentRun,
  AcpTaskData,
  AcpTodoItem,
} from "@/lib/acp/types";

export function PlanRow({
  title,
  steps,
}: {
  title: string;
  steps: AcpPlanStep[];
}) {
  const doneCount = steps.filter((s) => s.status === "done").length;
  const isRunning = steps.some((s) => s.status === "running");

  return (
    <TraceCollapsible defaultOpen>
      <TraceCollapsibleTrigger>
        <TraceLine status={isRunning ? "running" : undefined}>
          <TraceIcon>
            <RouteIcon />
          </TraceIcon>
          <TraceTitle>{title}</TraceTitle>
          <TraceDescription>
            {doneCount} of {steps.length} done
          </TraceDescription>
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent>
        <Trace>
          {steps.map((step, idx) => (
            <TraceLine
              key={`${step.title}-${idx}`}
              status={
                step.status === "pending"
                  ? "pending"
                  : step.status === "running"
                    ? "running"
                    : undefined
              }
            >
              <TraceIcon>
                {step.status === "done" ? (
                  <CircleCheckIcon />
                ) : step.status === "running" ? (
                  <CircleDotIcon />
                ) : (
                  <CircleIcon />
                )}
              </TraceIcon>
              <TraceTitle>{step.title}</TraceTitle>
            </TraceLine>
          ))}
        </Trace>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

export function TaskRow({ task }: { task: AcpTaskData }) {
  const [open, setOpen] = React.useState<boolean>();
  const hasError = task.steps.some((s) => s.status === "error");
  const isRunning = !hasError && task.steps.some((s) => s.status === "running");
  const status: "running" | "error" | undefined = hasError
    ? "error"
    : isRunning
      ? "running"
      : undefined;

  return (
    <TraceCollapsible open={open ?? isRunning} onOpenChange={setOpen}>
      <TraceCollapsibleTrigger>
        <TraceLine status={status}>
          <TraceIcon>
            {status === "running" ? (
              <LoaderCircleIcon className="animate-spin" />
            ) : status === "error" ? (
              <CircleAlertIcon />
            ) : (
              <CircleCheckIcon />
            )}
          </TraceIcon>
          <TraceTitle>{task.title}</TraceTitle>
        </TraceLine>
      </TraceCollapsibleTrigger>
      <TraceCollapsibleContent keepMounted>
        <Trace>
          {task.steps.map((step, idx) => (
            <TraceLine
              key={`${step.title}-${idx}`}
              status={step.status === "done" ? undefined : step.status}
            >
              <TraceTitle>{step.title}</TraceTitle>
            </TraceLine>
          ))}
        </Trace>
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

function TodoRow({ todo }: { todo: AcpTodoItem }) {
  const line = (
    <TraceLine status={todo.status === "in_progress" ? "running" : undefined}>
      <TraceIcon>
        {todo.status === "completed" ? (
          <CircleCheckIcon />
        ) : todo.status === "in_progress" ? (
          <CircleDotIcon />
        ) : (
          <CircleIcon />
        )}
      </TraceIcon>
      <TraceTitle>{todo.content}</TraceTitle>
    </TraceLine>
  );

  if (!todo.description) {
    return line;
  }

  return (
    <TraceCollapsible>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent className="text-xs">
        {todo.description}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}

export function TodoQueue({ todos }: { todos: AcpTodoItem[] }) {
  const pending = todos.filter((t) => t.status !== "completed");
  const completed = todos.filter((t) => t.status === "completed");

  if (todos.length === 0) {
    return null;
  }

  return (
    <Trace>
      {pending.length > 0 && (
        <TraceCollapsible defaultOpen>
          <TraceCollapsibleTrigger>
            <TraceLine>
              <TraceIcon>
                <ListTodoIcon />
              </TraceIcon>
              <TraceTitle>{pending.length} to do</TraceTitle>
            </TraceLine>
          </TraceCollapsibleTrigger>
          <TraceCollapsibleContent keepMounted>
            <Trace>
              {pending.map((todo, idx) => (
                <TodoRow key={`${todo.content}-${idx}`} todo={todo} />
              ))}
            </Trace>
          </TraceCollapsibleContent>
        </TraceCollapsible>
      )}
      {completed.length > 0 && (
        <TraceCollapsible>
          <TraceCollapsibleTrigger>
            <TraceLine>
              <TraceIcon>
                <ListChecksIcon />
              </TraceIcon>
              <TraceTitle>{completed.length} completed</TraceTitle>
            </TraceLine>
          </TraceCollapsibleTrigger>
          <TraceCollapsibleContent keepMounted>
            <Trace>
              {completed.map((todo, idx) => (
                <TodoRow key={`${todo.content}-${idx}`} todo={todo} />
              ))}
            </Trace>
          </TraceCollapsibleContent>
        </TraceCollapsible>
      )}
    </Trace>
  );
}

export function SubagentRow({ subagent }: { subagent: AcpSubagentRun }) {
  const isDone = subagent.status === "done";
  const isRunning = subagent.status === "running";
  const line = (
    <TraceLine status={isDone ? undefined : isRunning ? "running" : "error"}>
      <TraceIcon>
        {isDone ? (
          <CircleCheckIcon />
        ) : isRunning ? (
          <LoaderCircleIcon className="animate-spin" />
        ) : (
          <CircleAlertIcon />
        )}
      </TraceIcon>
      <TraceTitle>
        {isDone ? "Ran" : isRunning ? "Running" : "Failed to run"}{" "}
        {subagent.name}
      </TraceTitle>
      {subagent.durationSec !== undefined && (
        <TraceDescription>{subagent.durationSec}s</TraceDescription>
      )}
    </TraceLine>
  );

  if (subagent.steps.length === 0 && !subagent.report && !subagent.task) {
    return line;
  }

  return (
    <TraceCollapsible defaultOpen={isRunning}>
      <TraceCollapsibleTrigger>{line}</TraceCollapsibleTrigger>
      <TraceCollapsibleContent keepMounted className="flex flex-col gap-2.5">
        {(subagent.model || (subagent.tools && subagent.tools.length > 0)) && (
          <div className="flex flex-wrap gap-1.5">
            {subagent.model && (
              <Badge variant="secondary">{subagent.model}</Badge>
            )}
            {subagent.tools?.map((toolName) => (
              <Badge key={toolName} variant="outline" className="font-mono">
                {toolName}
              </Badge>
            ))}
          </div>
        )}
        {subagent.task && <p className="text-xs">{subagent.task}</p>}
        {subagent.steps.length > 0 && (
          <Trace>
            {subagent.steps.map((step) =>
              step.output ? (
                <TraceCollapsible key={step.id}>
                  <TraceCollapsibleTrigger>
                    <TraceLine
                      status={
                        step.status === "done" ? undefined : step.status
                      }
                    >
                      <TraceIcon>
                        <CircleCheckIcon />
                      </TraceIcon>
                      <TraceTitle>Ran {step.command}</TraceTitle>
                    </TraceLine>
                  </TraceCollapsibleTrigger>
                  <TraceCollapsibleContent>
                    <CodeBlock size="sm">
                      <CodeBlockContent wrap className="max-h-48">
                        {step.output}
                      </CodeBlockContent>
                    </CodeBlock>
                  </TraceCollapsibleContent>
                </TraceCollapsible>
              ) : (
                <TraceLine
                  key={step.id}
                  status={step.status === "done" ? undefined : step.status}
                >
                  <TraceIcon>
                    {step.status === "running" ? (
                      <LoaderCircleIcon className="animate-spin" />
                    ) : step.status === "error" ? (
                      <CircleAlertIcon />
                    ) : (
                      <CircleCheckIcon />
                    )}
                  </TraceIcon>
                  <TraceTitle>
                    {step.status === "running"
                      ? "Running"
                      : step.status === "error"
                        ? "Failed to run"
                        : "Ran"}{" "}
                    {step.command}
                  </TraceTitle>
                </TraceLine>
              ),
            )}
          </Trace>
        )}
        {subagent.report && (
          <p className="text-xs text-foreground">{subagent.report}</p>
        )}
      </TraceCollapsibleContent>
    </TraceCollapsible>
  );
}
