import "@xyflow/react/style.css";
import { ThemeProvider } from "@/components/theme-provider";
import { ModeToggle } from "./components/mode-toggle";
import { useState, useCallback } from "react";
import {
  ReactFlow,
  addEdge,
  Background,
  Controls,
  MiniMap,
} from "@xyflow/react";
import { BackgroundVariant } from "@xyflow/react";

function App() {
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
    (params: any) =>
      setEdges((edgesSnapshot) => addEdge(params, edgesSnapshot)),
    [],
  );

  return (
    <ThemeProvider
      defaultTheme="dark"
      storageKey="vite-ui-theme"
    >
      <ModeToggle />
      <div style={{ width: "100vw", height: "94vh" }}>
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
    </ThemeProvider>
  );
}

export default App;
