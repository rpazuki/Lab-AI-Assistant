"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Markdown } from "@/components/ask/Markdown";
import { SessionDrawer } from "@/components/ask/SessionDrawer";
import { type ContextState, SourceCard } from "@/components/ask/SourceCard";
import { Icon } from "@/components/ui/icons";
import { Alert, Button, Empty, Panel, rowList } from "@/components/ui/primitives";
import {
  ApiError,
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
import { usePager } from "@/lib/usePager";
import type {
  Allowance,
  AskEvent,
  ChatMessage,
  ChatSession,
  Citation,
  Page,
  SourceBlock,
} from "@/types";

interface Turn {
  question: string;
  answer: string;
  sources: SourceBlock[];
  /** True once retrieval has answered, even with no sources. */
  retrieved: boolean;
  citations: Citation[];
  unsupported: string[];
  messageId: string | null;
  releaseId: string | null;
  releaseLabel: string | null;
  latencyMs: number | null;
  error: string | null;
}

function blankTurn(question: string): Turn {
  return {
    question,
    answer: "",
    sources: [],
    retrieved: false,
    citations: [],
    unsupported: [],
    messageId: null,
    releaseId: null,
    releaseLabel: null,
    latencyMs: null,
    error: null,
  };
}

function seconds(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/**
 * The Ask surface.
 *
 * Sources appear before the prose does, so a reader can see what the answer is
 * grounded in while it is still being written. The answer is light Markdown; each
 * citation in it is a number that points at its source, and a cited source opens
 * the exact passage in its paper. An unsupported citation is shown as flagged
 * rather than hidden: a wrong citation the reader can see is far safer than one
 * they cannot.
 *
 * The conversations are in a drawer that opens over the page, so the answer and
 * its sources have the width.
 */
export function AskClient({ initialSessionId }: { initialSessionId?: string }) {
  const [sessionPage, setSessionPage] = useState<Page<ChatSession>>({
    items: [],
    next_cursor: null,
    total: 0,
  });
  const sessions = sessionPage.items;
  const sessionPager = usePager(20);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId ?? null);
  const [turns, setTurns] = useState<Turn[]>([]);
  // Whose sources the panel shows; null follows the latest question.
  const [shownTurn, setShownTurn] = useState<number | null>(null);
  const [opened, setOpened] = useState<Record<string, ContextState>>({});
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const cards = useRef<Record<string, HTMLLIElement | null>>({});
  const [question, setQuestion] = useState("");
  const [streaming, setStreaming] = useState(false);
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
      setSessionPage(
        await listSessions({ after: sessionPager.cursor, limit: sessionPager.pageSize }),
      );
    } catch {
      // The list is not worth failing the page for.
    }
  }, [sessionPager.cursor, sessionPager.pageSize]);

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

  function openSession(id: string | null) {
    setSessionId(id);
    setTurns([]);
    setShownTurn(null);
    setOpened({});
    setHighlighted(null);
  }

  const shownIndex = shownTurn ?? turns.length - 1;
  const shown = turns[shownIndex] as Turn | undefined;
  const currentTitle = sessions.find((session) => session.id === sessionId)?.title;

  async function onAsk(event?: React.FormEvent) {
    event?.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || streaming) return;

    let activeSession = sessionId;
    if (!activeSession) {
      activeSession = (await createSession()).id;
      setSessionId(activeSession);
    }

    const index = turns.length;
    setTurns((current) => [...current, blankTurn(trimmed)]);
    setShownTurn(null);
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
            update({
              sources: streamed.data.sources,
              retrieved: true,
              releaseId: streamed.data.release_id,
              releaseLabel: streamed.data.release_label ?? null,
            });
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
      const stopped = caught instanceof DOMException && caught.name === "AbortError";
      update({
        error: stopped
          ? "You stopped this answer, so it was not saved."
          : caught instanceof Error
            ? caught.message
            : "the answer stream failed",
      });
    } finally {
      setStreaming(false);
      streamingRef.current = false;
      abort.current = null;
      void refreshSessions();
      void refreshAllowance();
    }
  }

  async function openContext(turn: Turn, handle: string) {
    if (!turn.messageId) return;
    const key = `${turn.messageId}|${handle}`;
    setOpened((current) => ({ ...current, [key]: { state: "loading" } }));
    try {
      const context = await resolveCitation(turn.messageId, handle);
      setOpened((current) => ({ ...current, [key]: { state: "open", context } }));
    } catch (caught) {
      setOpened((current) => ({
        ...current,
        [key]: {
          state: "failed",
          reason:
            caught instanceof ApiError || caught instanceof Error
              ? caught.message
              : "The citation could not be opened.",
        },
      }));
    }
  }

  function closeContext(turn: Turn, handle: string) {
    const key = `${turn.messageId}|${handle}`;
    setOpened((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  /** A citation in the answer: show its source, opened in context. */
  function goToSource(index: number, handle: string) {
    const turn = turns[index];
    setShownTurn(index === turns.length - 1 ? null : index);
    setHighlighted(handle);
    if (turn && turn.citations.some((c) => c.handle === handle) && turn.messageId) {
      if (!opened[`${turn.messageId}|${handle}`]) void openContext(turn, handle);
    }
    // After the panel has rendered this turn's cards.
    window.setTimeout(() => {
      cards.current[handle]?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    }, 0);
  }

  return (
    <div className="space-y-4">
      <div className="relative flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          aria-expanded={drawerOpen}
          aria-haspopup="dialog"
          onClick={() => setDrawerOpen((open) => !open)}
        >
          <span className="inline-flex items-center gap-1.5">
            <Icon name="history" />
            Sessions
            {sessionPage.total > 0 ? (
              <span className="text-muted">({sessionPage.total})</span>
            ) : null}
          </span>
        </Button>
        <span className="min-w-0 flex-1 truncate text-sm text-muted">
          {sessionId ? (currentTitle ?? "Untitled session") : "New conversation"}
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            openSession(null);
            setDrawerOpen(false);
          }}
          disabled={streaming}
        >
          <span className="inline-flex items-center gap-1.5">
            <Icon name="plus" />
            New session
          </span>
        </Button>
        <SessionDrawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          sessions={sessions}
          total={sessionPage.total}
          nextCursor={sessionPage.next_cursor}
          pager={sessionPager}
          currentId={sessionId}
          onChoose={(session) => {
            if (session.id !== sessionId) openSession(session.id);
            setDrawerOpen(false);
          }}
          onRename={async (session) => {
            const title = window.prompt("Session title", session.title ?? "");
            if (title) {
              await renameSession(session.id, title);
              void refreshSessions();
            }
          }}
          onDelete={async (session) => {
            await deleteSession(session.id);
            if (session.id === sessionId) openSession(null);
            void refreshSessions();
          }}
        />
      </div>

      <div className="grid items-start gap-6 @4xl:grid-cols-[minmax(0,1fr)_22rem] @6xl:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="min-w-0 space-y-4">
          {turns.length === 0 ? (
            <Panel title="Ask">
              <Empty>
                Ask a question about the published corpus. Answers cite verified evidence, and
                every citation opens the exact passage it came from.
              </Empty>
            </Panel>
          ) : null}

          {turns.map((turn, index) => (
            <TurnCard
              key={index}
              turn={turn}
              selected={turns.length > 1 && index === shownIndex}
              writing={streaming && index === turns.length - 1}
              onShowSources={() => setShownTurn(index === turns.length - 1 ? null : index)}
              onCitation={(handle) => goToSource(index, handle)}
            />
          ))}

          <form
            onSubmit={onAsk}
            className="sticky bottom-3 z-10 space-y-2 rounded-card border border-border bg-surface p-3 shadow-card"
          >
            <div className="flex items-end gap-2">
              <textarea
                aria-label="Question"
                rows={Math.min(8, Math.max(2, question.split("\n").length))}
                placeholder="e.g., What lipid yield was reported for the engineered strain?"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void onAsk();
                  }
                }}
                className="min-h-10 w-full resize-none rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-ink shadow-control placeholder:text-muted focus:border-primary focus-visible:outline-offset-0"
              />
              {streaming ? (
                <Button type="button" variant="ghost" onClick={() => abort.current?.abort()}>
                  Stop
                </Button>
              ) : (
                <Button type="submit" disabled={Boolean(allowance?.limit_reached)}>
                  Ask
                </Button>
              )}
            </div>
            <p className="flex flex-wrap justify-between gap-2 px-1 text-xs text-muted">
              <span>{streaming ? "Answering…" : "Enter asks; Shift+Enter starts a new line."}</span>
              {allowance ? (
                <span className="tabular-nums">
                  {allowance.used_tokens.toLocaleString()} of{" "}
                  {allowance.token_limit.toLocaleString()} tokens used
                </span>
              ) : null}
            </p>
          </form>
          {visibleToAdmins ? (
            <p className="px-1 text-xs text-muted">
              Admins of this workspace can read your conversations. Every read is recorded.
            </p>
          ) : null}
          {allowance?.limit_reached ? (
            <Alert tone="warning" role="status">
              You have used your token allowance of {allowance.token_limit.toLocaleString()}. Ask
              an admin of this workspace to raise it.
            </Alert>
          ) : null}
        </div>

        <div className="min-w-0 @4xl:sticky @4xl:top-4">
          <Panel
            title="Sources"
            flush
            aside={
              shown && turns.length > 1 ? (
                <span className="text-xs text-muted">
                  Question {shownIndex + 1} of {turns.length}
                </span>
              ) : null
            }
          >
            <div className="@4xl:max-h-[calc(100vh-9rem)] @4xl:overflow-y-auto">
              {shown && turns.length > 1 ? (
                <p className="border-b border-border px-5 py-2 text-xs text-muted line-clamp-2">
                  For: {shown.question}
                </p>
              ) : null}
              {shown?.sources.some((source) => source.abstract_only) ? (
                <div className="px-5 pt-3">
                  <Alert tone="info" role="status">
                    Some of these sources are abstracts only: their full texts could not be
                    fetched, so methods and full results were not read. The answer says which
                    points rest on them.
                  </Alert>
                </div>
              ) : null}
              {shown?.sources.length ? (
                <ol className={rowList} aria-label="Sources">
                  {shown.sources.map((source, position) => {
                    const citation = shown.citations.find((c) => c.handle === source.handle);
                    const key = `${shown.messageId}|${source.handle}`;
                    return (
                      <SourceCard
                        key={source.handle}
                        ref={(element) => {
                          cards.current[source.handle] = element;
                        }}
                        source={source}
                        number={position + 1}
                        citation={citation ?? null}
                        opened={opened[key]}
                        highlighted={highlighted === source.handle}
                        onOpen={shown.messageId ? () => void openContext(shown, source.handle) : undefined}
                        onClose={() => closeContext(shown, source.handle)}
                      />
                    );
                  })}
                </ol>
              ) : (
                <div className="px-5 py-3">
                  <Empty>
                    {shown?.retrieved || (shown?.messageId && !streaming)
                      ? "No sources were found for this question."
                      : "Sources appear here as soon as retrieval finishes."}
                  </Empty>
                </div>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function TurnCard({
  turn,
  selected,
  writing,
  onShowSources,
  onCitation,
}: {
  turn: Turn;
  selected: boolean;
  writing: boolean;
  onShowSources: () => void;
  onCitation: (handle: string) => void;
}) {
  const numbers = useMemo(
    () => new Map(turn.sources.map((source, index) => [source.handle, index + 1])),
    [turn.sources],
  );
  const renderCitation = (handle: string, key: string): ReactNode => {
    const number = numbers.get(handle);
    const citation = turn.citations.find((c) => c.handle === handle);
    const flagged =
      number === undefined ||
      turn.unsupported.includes(handle) ||
      (citation !== undefined && citation.status !== "valid");
    return (
      <button
        key={key}
        type="button"
        onClick={() => onCitation(handle)}
        aria-label={`Source ${number ?? "not supplied"}, ${handle}`}
        title={handle}
        className={`mx-0.5 inline-flex h-5 min-w-5 -translate-y-px items-center justify-center rounded-badge border px-1 align-baseline font-sans text-[0.7rem] font-semibold leading-none transition-colors ${
          flagged
            ? "border-warning-strong/25 bg-warning-lt text-warning-strong"
            : "border-border-strong bg-surface text-link hover:bg-success-lt"
        }`}
      >
        {number ?? "?"}
      </button>
    );
  };

  return (
    <article
      className={`overflow-hidden rounded-card border bg-surface shadow-card ${
        selected ? "border-accent ring-1 ring-accent" : "border-border"
      }`}
    >
      <header className="border-b border-border bg-surface-subtle px-5 py-3">
        <p className="whitespace-pre-wrap font-semibold text-heading">{turn.question}</p>
      </header>
      <div className="space-y-3 px-5 py-4">
        {turn.error ? (
          <Alert tone="danger" role="alert">
            {turn.error}
          </Alert>
        ) : null}
        {turn.answer ? (
          <Markdown text={turn.answer} renderCitation={renderCitation} />
        ) : (
          <p className="text-sm text-muted">
            {writing
              ? turn.retrieved
                ? "Writing the answer…"
                : "Finding sources…"
              : turn.error
                ? ""
                : "No answer was produced."}
          </p>
        )}
        {turn.unsupported.length > 0 ? (
          <Alert tone="warning" role="status">
            This answer cited {turn.unsupported.length} handle
            {turn.unsupported.length === 1 ? "" : "s"} that the server did not supply. Those
            citations are flagged and should not be trusted.
          </Alert>
        ) : null}
      </div>
      {turn.messageId ? (
        <footer className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-2.5 text-xs text-muted">
          {turn.sources.length > 0 ? (
            <button
              type="button"
              className="font-medium text-link underline hover:no-underline"
              onClick={onShowSources}
            >
              {turn.sources.length} source{turn.sources.length === 1 ? "" : "s"}
            </button>
          ) : null}
          {turn.releaseLabel || turn.releaseId ? (
            <span>
              Release{" "}
              {turn.releaseLabel ?? <span className="font-mono">{turn.releaseId!.slice(0, 8)}</span>}
            </span>
          ) : null}
          {turn.latencyMs !== null ? <span>{seconds(turn.latencyMs)}</span> : null}
          <span className="ml-auto flex gap-3">
            <button
              type="button"
              className="text-link underline hover:no-underline"
              onClick={() => void sendFeedback(turn.messageId!, 5)}
            >
              useful
            </button>
            <button
              type="button"
              className="text-link underline hover:no-underline"
              onClick={() => void sendFeedback(turn.messageId!, 1, "not useful")}
            >
              not useful
            </button>
          </span>
        </footer>
      ) : null}
    </article>
  );
}

function toTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      turns.push(blankTurn(message.content));
      continue;
    }
    const current = turns.at(-1);
    if (!current) continue;
    current.answer = message.content;
    current.sources = message.sources ?? [];
    current.retrieved = true;
    current.citations = message.citations;
    current.unsupported = message.unsupported_handles;
    current.messageId = message.id;
    current.releaseId = message.release_id;
    current.releaseLabel = message.release_label ?? null;
    current.latencyMs = message.latency_ms;
  }
  return turns;
}
