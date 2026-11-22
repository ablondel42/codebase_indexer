import "@xyflow/react/style.css";
import { useCallback, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  addEdge,
  type Connection,
} from "@xyflow/react";

import { AcpSessionsPanel } from "@/components/acp-sessions-panel";
import { AcpStatusBar } from "@/components/acp-status-bar";
import { ModeToggle } from "@/components/mode-toggle";
import { ThemeProvider } from "@/components/theme-provider";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useAcpConnection } from "@/hooks/use-acp-connection";

function WorkspaceCanvas() {
  const acp = useAcpConnection();
  const [sessionsPanelOpen, setSessionsPanelOpen] = useState(true);

  const initialNodes = [
    { id: "n1", position: { x: 0, y: 0 }, data: { label: "Node 1" } },
    { id: "n2", position: { x: 0, y: 100 }, data: { label: "Node 2" } },
  ];
  const initialEdges = [{ id: "n1-n2", source: "n1", target: "n2" }];
  const [nodes, setNodes] = useState(initialNodes);
  const [edges, setEdges] = useState(initialEdges);

  const onNodesChange = useCallback(
    (changes: {
      applyTo: (
        arg0: {
          id: string;
          position: { x: number; y: number };
          data: { label: string };
        }[],
      ) => {
        id: string;
        position: { x: number; y: number };
        data: { label: string };
      }[];
    }) => setNodes((nodesSnapshot) => changes.applyTo(nodesSnapshot)),
    [],
  );
  const onEdgesChange = useCallback(
    (changes: {
      applyTo: (
        arg0: { id: string; source: string; target: string }[],
      ) => { id: string; source: string; target: string }[];
    }) => setEdges((edgesSnapshot) => changes.applyTo(edgesSnapshot)),
    [],
  );
  const onConnect = useCallback(
    (params: Connection) =>
      setEdges((edgesSnapshot) => addEdge(params, edgesSnapshot)),
    [],
  );

  const showPanel =
    acp.status === "ready" &&
    acp.authStatus === "authenticated" &&
    sessionsPanelOpen;

  return (
    <div className="flex h-screen w-screen flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border bg-background px-4">
        <AcpStatusBar
          status={acp.status}
          result={acp.result}
          authStatus={acp.authStatus}
          activeMethodId={acp.activeMethodId}
          authenticatingMethodId={acp.authenticatingMethodId}
          canLogout={acp.canLogout}
          accountTier={acp.accountTier}
          sessionCount={acp.sessions.length}
          activeSessionId={acp.activeSessionId}
          sessionsPanelOpen={sessionsPanelOpen}
          onToggleSessionsPanel={() =>
            setSessionsPanelOpen((current) => !current)
          }
          error={acp.error}
          authError={acp.authError}
          onClearAuthError={acp.clearAuthError}
          onAuthenticate={acp.authenticate}
          onLogout={acp.logout}
          onReconnect={acp.reconnect}
        />
        <div className="flex shrink-0 items-center gap-2.5 self-stretch py-2">
          <Separator orientation="vertical" />
          <ModeToggle />
        </div>
      </header>
      <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <ResizablePanel
          id="workspace-canvas"
          defaultSize={showPanel ? "66%" : "100%"}
          minSize="35%"
        >
          <div className="size-full">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              fitView
            >
              <Controls />
              <MiniMap />
              <Background variant={BackgroundVariant.Dots} gap={12} size={1} />
            </ReactFlow>
          </div>
        </ResizablePanel>
        {showPanel && (
          <>
            <ResizableHandle withHandle />
            <ResizablePanel
              id="acp-sessions-window"
              defaultSize="34%"
              minSize="24%"
              maxSize="65%"
            >
              <AcpSessionsPanel
                defaultCwd={acp.defaultCwd}
                agyProfileDir={acp.agyProfileDir}
                includeCbiMcp={acp.includeCbiMcp}
                onToggleCbiMcp={acp.setIncludeCbiMcp}
                sessions={acp.sessions}
                nextCursor={acp.nextCursor}
                activeSessionId={acp.activeSessionId}
                activeSessionMode={acp.activeSessionMode}
                replayedMessages={acp.replayedMessages}
                hasMoreMessages={acp.hasMoreMessages}
                loadingMessages={acp.loadingMessages}
                loadingOlderMessages={acp.loadingOlderMessages}
                isLiveThinking={acp.isLiveThinking}
                liveTurnActive={acp.liveTurnActive}
                liveTurnStartedAtMs={acp.liveTurnStartedAtMs}
                liveItemIds={acp.liveItemIds}
                totalMessageItems={acp.totalMessageItems}
                onLoadOlderMessages={acp.loadOlderMessages}
                sessionModels={acp.sessionModels}
                sessionModes={acp.sessionModes}
                configOptions={acp.configOptions}
                availableCommands={acp.availableCommands}
                sessionUsage={acp.sessionUsage}
                configBusyId={acp.configBusyId}
                onSetConfigOption={acp.setSessionConfigOption}
                onSetMode={acp.setSessionMode}
                sessionArtifacts={acp.sessionArtifacts}
                selectedArtifact={acp.selectedArtifact}
                artifactsBusy={acp.artifactsBusy}
                sessionCaps={acp.sessionCaps}
                sessionBusyId={acp.sessionBusyId}
                sessionError={acp.sessionError}
                onClearSessionError={acp.clearSessionError}
                onRefresh={acp.refreshSessions}
                onLoadMore={acp.loadMoreSessions}
                onCreateSession={acp.createNewSession}
                onResumeSession={acp.resumeExistingSession}
                onCloseSession={acp.closeActiveSession}
                onRefreshArtifacts={acp.refreshArtifacts}
                onOpenArtifact={acp.openArtifact}
                onCloseArtifact={acp.closeArtifact}
              />
            </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
    </div>
  );
}

function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="vite-ui-theme">
      <TooltipProvider>
        <WorkspaceCanvas />
      </TooltipProvider>
    </ThemeProvider>
  );
}

export default App;
