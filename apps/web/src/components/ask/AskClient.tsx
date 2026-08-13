"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Button, Empty, Input, Panel, StateChip } from "@/components/ui/primitives";
import {
  ask,
  createSession,
  deleteSession,
  getSession,
  listSessions,
  renameSession,
  resolveCitation,
  sendFeedback,
} from "@/lib/api";
import type { AskEvent, ChatMessage, ChatSession, Citation, SourceBlock } from "@/types";

interface Turn {
  question: string;
  answer: string;
  sources: SourceBlock[];
  citations: Citation[];
  unsupported: string[];
  messageId: string | null;
  releaseId: string | null;
  latencyMs: number | null;
  error: string | null;
}

/**
 * The Ask surface.
 *
 * Sources appear before the prose does, so a reader can see what the answer is
 * grounded in while it is still being written. A citation opens the exact evidence
 * in its source context, and an unsupported citation is shown as flagged rather
 * than hidden — a wrong citation the reader can see is far safer than one they
 * cannot.
 */
export function AskClient({ initialSessionId }: { initialSessionId?: string }) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId ?? null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const abort = useRef<AbortController | null>(null);
  //: Read inside the hydration effect, which must not fight an in-flight answer.
  const streamingRef = useRef(false);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await listSessions());
    } catch {
      // The sidebar is not worth failing the page for.
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    if (!sessionId || streamingRef.current) return;
    void (async () => {
      try {
        const session = await getSession(sessionId);
        const history = toTurns(session.messages);
        // Hydrate from history only when there is history. A freshly created
        // session returns no messages, and replacing state with that empty list
        // would erase the turn the user just started.
        if (history.length > 0) setTurns(history);
      } catch {
        // Leave whatever is on screen: a failed history load is not a reason to
        // discard an answer the user is reading.
      }
    })();
  }, [sessionId]);

  async function onAsk(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || streaming) return;

    let activeSession = sessionId;
    if (!activeSession) {
      activeSession = (await createSession()).id;
      setSessionId(activeSession);
    }

    const index = turns.length;
    setTurns((current) => [
      ...current,
      {
        question: trimmed,
        answer: "",
        sources: [],
        citations: [],
        unsupported: [],
        messageId: null,
        releaseId: null,
        latencyMs: null,
        error: null,
      },
    ]);
    setQuestion("");
    setStreaming(true);
    streamingRef.current = true;
    abort.current = new AbortController();

    const update = (patch: Partial<Turn>) =>
      setTurns((current) =>
        current.map((turn, position) => (position === index ? { ...turn, ...patch } : turn)),
      );

    try {
      await ask(
        activeSession,
        trimmed,
        (streamed: AskEvent) => {
          if (streamed.type === "sources") {
            update({ sources: streamed.data.sources, releaseId: streamed.data.release_id });
          } else if (streamed.type === "token") {
            setTurns((current) =>
              current.map((turn, position) =>
                position === index ? { ...turn, answer: turn.answer + streamed.data } : turn,
              ),
            );
          } else if (streamed.type === "done") {
            update({
              messageId: streamed.data.message_id,
              citations: streamed.data.citations,
              unsupported: streamed.data.unsupported_handles,
              latencyMs: streamed.data.latency_ms,
            });
          } else {
            update({ error: streamed.data.detail });
          }
        },
        { signal: abort.current.signal },
      );
    } catch (caught) {
      update({ error: caught instanceof Error ? caught.message : "the answer stream failed" });
    } finally {
      setStreaming(false);
      streamingRef.current = false;
      abort.current = null;
      void refreshSessions();
    }
  }

  async function onOpenCitation(messageId: string | null, handle: string) {
    if (!messageId) return;
    try {
      setEvidence(await resolveCitation(messageId, handle));
    } catch (caught) {
      setEvidence({
        resolved: false,
        reason: caught instanceof Error ? caught.message : "citation could not be opened",
      });
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_1fr_18rem]">
      <Panel
        title="Sessions"
        aside={
          <Button
            variant="ghost"
            onClick={async () => {
              const session = await createSession();
              setSessionId(session.id);
              setTurns([]);
              void refreshSessions();
            }}
          >
            New
          </Button>
        }
      >
        {sessions.length === 0 ? (
          <Empty>No sessions yet.</Empty>
        ) : (
          <ul className="space-y-1 text-sm">
            {sessions.map((session) => (
              <li key={session.id} className="flex items-center gap-1">
                <button
                  className={`flex-1 truncate rounded px-2 py-1 text-left hover:bg-neutral-100 ${
                    session.id === sessionId ? "bg-neutral-100 font-medium" : ""
                  }`}
                  onClick={() => setSessionId(session.id)}
                >
                  {session.title ?? "Untitled session"}
                </button>
                <button
                  aria-label={`Rename ${session.title ?? "session"}`}
                  className="px-1 text-xs text-neutral-500 hover:text-neutral-900"
                  onClick={async () => {
                    const title = window.prompt("Session title", session.title ?? "");
                    if (title) {
                      await renameSession(session.id, title);
                      void refreshSessions();
                    }
                  }}
                >
                  rename
                </button>
                <button
                  aria-label={`Delete ${session.title ?? "session"}`}
                  className="px-1 text-xs text-red-600 hover:text-red-800"
                  onClick={async () => {
                    await deleteSession(session.id);
                    if (session.id === sessionId) {
                      setSessionId(null);
                      setTurns([]);
                    }
                    void refreshSessions();
                  }}
                >
                  delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <div className="space-y-4">
        {turns.length === 0 ? (
          <Panel title="Ask">
            <Empty>
              Ask a question about the published corpus. Answers cite verified evidence, and
              every citation opens the exact passage it came from.
            </Empty>
          </Panel>
        ) : null}

        {turns.map((turn, index) => (
          <article key={index} className="space-y-3">
            <p className="font-medium">{turn.question}</p>
            {turn.error ? (
              <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                {turn.error}
              </p>
            ) : null}
            <div className="whitespace-pre-wrap rounded-lg border border-neutral-200 bg-white p-4 text-sm leading-relaxed">
              {turn.answer || (streaming ? "…" : "No answer was produced.")}
            </div>
            {turn.unsupported.length > 0 ? (
              <p
                role="status"
                className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
              >
                This answer cited {turn.unsupported.length} handle
                {turn.unsupported.length === 1 ? "" : "s"} that the server did not supply. Those
                citations are flagged and should not be trusted.
              </p>
            ) : null}
            {turn.citations.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {turn.citations.map((citation) => (
                  <button
                    key={citation.handle}
                    onClick={() => onOpenCitation(turn.messageId, citation.handle)}
                    className="rounded border border-neutral-300 px-2 py-1 font-mono text-xs hover:bg-neutral-100"
                  >
                    {citation.handle}
                    {citation.status !== "valid" ? ` · ${citation.status}` : ""}
                  </button>
                ))}
              </div>
            ) : null}
            {turn.messageId ? (
              <div className="flex items-center gap-3 text-xs text-neutral-500">
                {turn.releaseId ? <span>release {turn.releaseId.slice(0, 8)}</span> : null}
                {turn.latencyMs !== null ? <span>{turn.latencyMs} ms</span> : null}
                <button
                  className="underline hover:no-underline"
                  onClick={() => void sendFeedback(turn.messageId!, 5)}
                >
                  useful
                </button>
                <button
                  className="underline hover:no-underline"
                  onClick={() => void sendFeedback(turn.messageId!, 1, "not useful")}
                >
                  not useful
                </button>
              </div>
            ) : null}
          </article>
        ))}

        <form onSubmit={onAsk} className="flex gap-2">
          <Input
            aria-label="Question"
            placeholder="What lipid yield was reported for the engineered strain?"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
          <Button type="submit" disabled={streaming}>
            {streaming ? "Answering…" : "Ask"}
          </Button>
        </form>
      </div>

      <Panel title="Sources">
        {turns.at(-1)?.sources.length ? (
          <ul className="space-y-3 text-sm">
            {turns.at(-1)!.sources.map((source) => (
              <li key={source.handle} className="space-y-1">
                <p className="font-mono text-xs text-neutral-500">{source.handle}</p>
                <p className="font-medium">{source.title ?? "Untitled"}</p>
                <p className="text-xs text-neutral-500">
                  {[source.source_label, source.year, source.locator].filter(Boolean).join(" · ")}
                </p>
                <p className="text-xs text-neutral-700">{source.snippet}</p>
                {source.display_restricted ? (
                  <StateChip state="excerpt_only" />
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <Empty>Sources appear here as soon as retrieval finishes.</Empty>
        )}
        {evidence ? (
          <div className="mt-4 rounded border border-neutral-200 bg-neutral-50 p-3 text-xs">
            <p className="mb-1 font-semibold">Evidence in context</p>
            <pre className="whitespace-pre-wrap break-words">
              {JSON.stringify(evidence, null, 2)}
            </pre>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}

function toTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      turns.push({
        question: message.content,
        answer: "",
        sources: [],
        citations: [],
        unsupported: [],
        messageId: null,
        releaseId: null,
        latencyMs: null,
        error: null,
      });
      continue;
    }
    const current = turns.at(-1);
    if (!current) continue;
    current.answer = message.content;
    current.citations = message.citations;
    current.unsupported = message.unsupported_handles;
    current.messageId = message.id;
    current.releaseId = message.release_id;
    current.latencyMs = message.latency_ms;
  }
  return turns;
}
