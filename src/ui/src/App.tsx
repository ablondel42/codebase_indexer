import "@xyflow/react/style.css";
import { AcpStatusBar } from "@/components/acp-status-bar";
import { ThemeProvider } from "@/components/theme-provider";
import { useAcpConnection } from "@/hooks/use-acp-connection";
import { ModeToggle } from "./components/mode-toggle";
import { useState, useCallback } from "react";
import {
  ReactFlow,
  addEdge,
  Background,
  Controls,
  MiniMap,
  type Connection,
} from "@xyflow/react";
import { BackgroundVariant } from "@xyflow/react";

function WorkspaceCanvas() {
  const { status, result, error, reconnect } = useAcpConnection();
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

  return (
    <div className="flex h-screen w-screen flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-4">
        <AcpStatusBar
          status={status}
          result={result}
          error={error}
          onReconnect={reconnect}
        />
        <ModeToggle />
      </header>
      <div className="min-h-0 flex-1">
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
          <Background
            variant={BackgroundVariant.Dots}
            gap={12}
            size={1}
          />
        </ReactFlow>
      </div>
    </div>
  );
}

function App() {
  return (
    <ThemeProvider
      defaultTheme="dark"
      storageKey="vite-ui-theme"
    >
      <WorkspaceCanvas />
    </ThemeProvider>
  );
}

export default App;
