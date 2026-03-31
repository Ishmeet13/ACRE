import { useEffect, useRef, useState } from "react";

interface UseWebSocketReturn {
  lastMessage: string | null;
  readyState: number;
  sendMessage: (msg: string) => void;
}

export function useWebSocket(url: string): UseWebSocketReturn {
  const wsRef = useRef<WebSocket | null>(null);
  const [lastMessage, setLastMessage] = useState<string | null>(null);
  const [readyState, setReadyState] = useState<number>(WebSocket.CONNECTING);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  function connect() {
    if (!url) return;
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setReadyState(WebSocket.OPEN);
        // Start keepalive pings
        const ping = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 20_000);
        ws.addEventListener("close", () => clearInterval(ping));
      };

      ws.onmessage = (e) => {
        if (e.data !== '{"event":"pong"}') {
          setLastMessage(e.data);
        }
      };

      ws.onclose = () => {
        setReadyState(WebSocket.CLOSED);
        // Reconnect after 3s
        reconnectTimer.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {}
  }

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [url]);

  const sendMessage = (msg: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(msg);
    }
  };

  return { lastMessage, readyState, sendMessage };
}
