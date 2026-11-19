"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Alert,
  Button,
  Empty,
  Input,
  Panel,
  StateChip,
  row,
  rowList,
} from "@/components/ui/primitives";
import {
  ask,
  createSession,
  deleteSession,
  getAllowance,
  getMe,
  getSession,
  listSessions,
  renameSession,
  resolveCitation,
  sendFeedback,
} from "@/lib/api";
import type { Allowance, AskEvent, ChatMessage, ChatSession, Citation, SourceBlock } from "@/types";

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

  const [allowance, setAllowance] = useState<Allowance | null>(null);
  const [visibleToAdmins, setVisibleToAdmins] = useState(false);

  const refreshAllowance = useCallback(async () => {
    try {
      setAllowance(await getAllowance());
    } catch {
      // The allowance line is informative; the API still enforces the limit.
    }
  }, []);

  useEffect(() => {
    void refreshAllowance();
    getMe()
      .then((me) => setVisibleToAdmins(Boolean(me.chat_visible_to_admins)))
      .catch(() => undefined);
  }, [refreshAllowance]);

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
      void refreshAllowance();
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
    <div className="grid items-start gap-6 @3xl:grid-cols-[16rem_minmax(0,1fr)] @5xl:grid-cols-[18rem_minmax(0,1fr)_20rem]">
      <Panel
        title="Sessions"
        flush
        aside={
          <Button
            variant="ghost"
            size="sm"
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
          <div className="px-5 py-3">
            <Empty>No sessions yet.</Empty>
          </div>
        ) : (
          <ul className="space-y-0.5 p-2 text-sm">
            {sessions.map((session) => (
              <li key={session.id} className="group flex items-center gap-1">
                <button
                  className={`flex-1 truncate rounded-control border-l-2 px-3 py-1.5 text-left transition-colors ${
                    session.id === sessionId
                      ? "border-accent bg-success-lt font-medium text-heading"
                      : "border-transparent text-ink hover:bg-surface-subtle"
                  }`}
                  onClick={() => setSessionId(session.id)}
                >
                  {session.title ?? "Untitled session"}
                </button>
                <button
                  aria-label={`Rename ${session.title ?? "session"}`}
                  className="rounded-badge px-1 text-xs text-muted hover:text-heading"
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
                  className="rounded-badge px-1 text-xs text-danger-strong hover:underline"
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
          <article
            key={index}
            className="overflow-hidden rounded-card border border-border bg-surface shadow-card"
          >
            <header className="border-b border-border bg-surface-subtle px-5 py-3">
              <p className="font-semibold text-heading">{turn.question}</p>
            </header>
            <div className="space-y-3 px-5 py-4">
              {turn.error ? (
                <Alert tone="danger" role="alert">
                  {turn.error}
                </Alert>
              ) : null}
              <div className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                {turn.answer || (streaming ? "…" : "No answer was produced.")}
              </div>
              {turn.unsupported.length > 0 ? (
                <Alert tone="warning" role="status">
                  This answer cited {turn.unsupported.length} handle
                  {turn.unsupported.length === 1 ? "" : "s"} that the server did not supply. Those
                  citations are flagged and should not be trusted.
                </Alert>
              ) : null}
              {turn.citations.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {turn.citations.map((citation) => (
                    <button
                      key={citation.handle}
                      onClick={() => onOpenCitation(turn.messageId, citation.handle)}
                      className={`rounded-badge border px-2 py-1 font-mono text-xs transition-colors ${
                        citation.status === "valid"
                          ? "border-border-strong bg-surface text-link hover:bg-success-lt"
                          : "border-warning-strong/25 bg-warning-lt text-warning-strong"
                      }`}
                    >
                      {citation.handle}
                      {citation.status !== "valid" ? ` · ${citation.status}` : ""}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            {turn.messageId ? (
              <footer className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-2.5 text-xs text-muted">
                {turn.releaseId ? (
                  <span className="font-mono">release {turn.releaseId.slice(0, 8)}</span>
                ) : null}
                {turn.latencyMs !== null ? <span>{turn.latencyMs} ms</span> : null}
                <span className="ml-auto flex gap-3">
                  <button
                    className="text-link underline hover:no-underline"
                    onClick={() => void sendFeedback(turn.messageId!, 5)}
                  >
                    useful
                  </button>
                  <button
                    className="text-link underline hover:no-underline"
                    onClick={() => void sendFeedback(turn.messageId!, 1, "not useful")}
                  >
                    not useful
                  </button>
                </span>
              </footer>
            ) : null}
          </article>
        ))}

        <form
          onSubmit={onAsk}
          className="flex gap-2 rounded-card border border-border bg-surface p-3 shadow-card"
        >
          <Input
            aria-label="Question"
            placeholder="What lipid yield was reported for the engineered strain?"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
          <Button type="submit" disabled={streaming || Boolean(allowance?.limit_reached)}>
            {streaming ? "Answering…" : "Ask"}
          </Button>
        </form>
        {allowance?.limit_reached ? (
          <Alert tone="warning" role="status">
            You have used your token allowance of {allowance.token_limit.toLocaleString()}. Ask an
            admin of this workspace to raise it.
          </Alert>
        ) : null}
        <p className="flex flex-wrap justify-between gap-2 px-1 text-xs text-muted">
          <span>
            {visibleToAdmins
              ? "Admins of this workspace can read your conversations. Every read is recorded."
              : ""}
          </span>
          {allowance ? (
            <span className="tabular-nums">
              {allowance.used_tokens.toLocaleString()} of {allowance.token_limit.toLocaleString()}{" "}
              tokens used
            </span>
          ) : null}
        </p>
      </div>

      {/* Below the conversation when there is room for only two columns. */}
      <div className="@3xl:col-span-2 @5xl:col-span-1">
      <Panel title="Sources" flush>
        {turns.at(-1)?.sources.length ? (
          <ul className={`${rowList} text-sm`}>
            {turns.at(-1)!.sources.map((source) => (
              <li key={source.handle} className={`${row} space-y-1`}>
                <p className="font-mono text-xs text-muted">{source.handle}</p>
                <p className="font-semibold text-heading">{source.title ?? "Untitled"}</p>
                <p className="text-xs text-muted">
                  {[source.source_label, source.year, source.locator].filter(Boolean).join(" · ")}
                </p>
                <p className="border-l-2 border-accent pl-2 text-xs text-ink">{source.snippet}</p>
                {source.display_restricted ? (
                  <StateChip state="excerpt_only" />
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <div className="px-5 py-3">
            <Empty>Sources appear here as soon as retrieval finishes.</Empty>
          </div>
        )}
        {evidence ? (
          <div className="border-t border-border bg-surface-subtle px-5 py-4 text-xs">
            <p className="mb-2 font-semibold text-heading">Evidence in context</p>
            <pre className="whitespace-pre-wrap break-words rounded-control border border-border bg-surface p-3 font-mono text-ink">
              {JSON.stringify(evidence, null, 2)}
            </pre>
          </div>
        ) : null}
      </Panel>
      </div>
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
