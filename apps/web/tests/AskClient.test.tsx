import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AskClient } from "@/components/ask/AskClient";
import type { SourceBlock } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listSessions: vi.fn(async () => ({ items: [], next_cursor: null, total: 0 })),
    getAllowance: vi.fn(async () => ({
      token_limit: 1_000_000,
      used_tokens: 1200,
      remaining_tokens: 998_800,
      limit_reached: false,
      custom_limit: false,
    })),
    getMe: vi.fn(async () => ({
      user_id: "u1",
      email: "member@lab.invalid",
      full_name: null,
      workspace: { id: "w1", slug: "rlalab", name: "RLA Lab" },
      role: "researcher",
      capabilities: ["ask"],
      chat_visible_to_admins: true,
    })),
    createSession: vi.fn(async () => ({
      id: "s1",
      title: null,
      pinned_release_id: null,
      created_at: null,
      updated_at: null,
    })),
    getSession: vi.fn(async () => ({
      id: "s1",
      title: null,
      pinned_release_id: null,
      created_at: null,
      updated_at: null,
      messages: [],
    })),
    resolveCitation: vi.fn(async () => ({
      resolved: true,
      span: null,
      nodes: [
        {
          node_id: "p-3",
          text: "Before it. Strain YL-A reached a lipid yield of 0.42 g/g substrate. After it.",
          kind: "paragraph",
          row: null,
          column: null,
        },
      ],
      place: {
        kind: "paragraph",
        section_path: ["Results", "Lipid accumulation"],
        paragraph: 3,
        paragraphs: 7,
        page: 4,
        paragraph_on_page: 2,
        paragraphs_on_page: 5,
      },
      release_label: "rla-corpus 1.1.0",
    })),
    sendFeedback: vi.fn(async () => ({ id: "f1" })),
    deleteSession: vi.fn(async () => undefined),
    ask: vi.fn(),
  };
});

const api = await import("@/lib/api");

const LIPID: SourceBlock = {
  handle: "E0123456789",
  title: "Lipid production in an oleaginous yeast",
  year: 2025,
  journal: "Metabolic Engineering",
  authors: ["Pazuki R", "Ledesma-Amaro R", "Smith J", "Jones K"],
  doi: "10.1234/example",
  pmid: "38000001",
  pmcid: null,
  source_label: "doi:10.1234/example",
  locator: "Results · p.4",
  section: "Results",
  page: 4,
  chunk_type: "paragraph",
  display_restricted: false,
  snippet: "Strain YL-A reached a lipid yield of 0.42 g/g substrate.",
  text: "Strain YL-A reached a lipid yield of 0.42 g/g substrate.",
};

function sourcesEvent(sources: SourceBlock[]) {
  return {
    type: "sources" as const,
    data: {
      release_id: "r1",
      release_label: "rla-corpus 1.1.0",
      classification: "evidence",
      sources,
      claims: [],
    },
  };
}

function doneEvent(handles: string[], unsupported: string[] = []) {
  return {
    type: "done" as const,
    data: {
      message_id: "m1",
      citations: handles.map((handle) => ({
        handle,
        status: unsupported.includes(handle) ? ("not_supplied" as const) : ("valid" as const),
        work_id: "w1",
        display_label: "Results",
        evidence_span_id: null,
      })),
      unsupported_handles: unsupported,
      citation_validation_passed: unsupported.length === 0,
      release_id: "r1",
      release_label: "rla-corpus 1.1.0",
      latency_ms: 4200,
    },
  };
}

async function askQuestion(text = "What lipid yield was reported?") {
  await userEvent.type(screen.getByLabelText(/question/i), text);
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
}

describe("AskClient", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.resetModules());

  it("shows sources, with their paper's details, before the answer text arrives", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([LIPID]));
    });

    render(<AskClient />);
    await askQuestion();

    const sources = await screen.findByRole("list", { name: "Sources" });
    expect(within(sources).getByText(/Lipid production in an oleaginous yeast/)).toBeInTheDocument();
    expect(within(sources).getByText("Pazuki R, Ledesma-Amaro R, Smith J et al.")).toBeInTheDocument();
    expect(within(sources).getByText("Metabolic Engineering · 2025")).toBeInTheDocument();
    expect(within(sources).getByText("Results · page 4")).toBeInTheDocument();
    const doi = within(sources).getByRole("link", { name: /DOI 10.1234\/example/ });
    expect(doi).toHaveAttribute("href", "https://doi.org/10.1234/example");
    expect(within(sources).getByRole("link", { name: /PMID 38000001/ })).toHaveAttribute(
      "href",
      "https://pubmed.ncbi.nlm.nih.gov/38000001/",
    );
  });

  it("marks a source that is only the paper's abstract, and says so above them", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([{ ...LIPID, abstract_only: true, chunk_type: "abstract" }]));
    });

    render(<AskClient />);
    await askQuestion("What do honeybees need?");

    expect(await screen.findByText(/Some of these sources are abstracts only/)).toBeInTheDocument();
    expect(screen.getByText("abstract_only")).toBeInTheDocument();
  });

  it("renders the live answer as Markdown, line breaks included", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([LIPID]));
      onEvent({ type: "token", data: "## Yield\n\nTwo strains were " });
      onEvent({ type: "token", data: "**engineered**:\n\n- YL-A: 0.42 g/g [E0123456789]\n" });
      onEvent({ type: "token", data: "- YL-B: not reported\n" });
      onEvent(doneEvent(["E0123456789"]));
    });

    render(<AskClient />);
    await askQuestion("yield?");

    expect(await screen.findByRole("heading", { name: "Yield" })).toBeInTheDocument();
    expect(screen.getByText("engineered").tagName).toBe("STRONG");
    const answer = screen.getByRole("heading", { name: "Yield" }).closest("article")!;
    const items = within(answer)
      .getAllByRole("listitem")
      .filter((item) => /YL-/.test(item.textContent ?? ""));
    expect(items.map((item) => item.textContent)).toEqual([
      "YL-A: 0.42 g/g 1",
      "YL-B: not reported",
    ]);
  });

  it("numbers each citation by its source and opens it in context", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([LIPID]));
      onEvent({ type: "token", data: "Strain YL-A reached 0.42 g/g [E0123456789]." });
      onEvent(doneEvent(["E0123456789"]));
    });

    render(<AskClient />);
    await askQuestion("yield?");

    const citation = await screen.findByRole("button", { name: "Source 1, E0123456789" });
    expect(citation).toHaveTextContent("1");
    await userEvent.click(citation);
    await waitFor(() => expect(api.resolveCitation).toHaveBeenCalledWith("m1", "E0123456789"));

    expect(await screen.findByText("Where in the paper")).toBeInTheDocument();
    expect(
      screen.getByText("The 3rd of 7 paragraphs of Results › Lipid accumulation."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Page 4: the 2nd of 5 paragraphs on the page, near the top."),
    ).toBeInTheDocument();
    // The cited passage is marked inside its paragraph.
    expect(
      screen.getByText(/Strain YL-A reached a lipid yield/, { selector: "mark" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/release rla-corpus 1.1.0/)).toBeInTheDocument();
    // No raw evidence dump, and no internal ids.
    expect(screen.queryByText(/rendition_id|work_id/)).not.toBeInTheDocument();
    // The answer's footer names the release, not its id.
    expect(screen.getByText(/Release rla-corpus 1.1.0/)).toBeInTheDocument();
  });

  it("shows a reopened conversation's sources again", async () => {
    vi.mocked(api.listSessions).mockResolvedValueOnce({
      items: [
        {
          id: "s9",
          title: "Lipid yields",
          pinned_release_id: null,
          created_at: null,
          updated_at: null,
        },
      ],
      next_cursor: null,
      total: 1,
    });
    vi.mocked(api.getSession).mockResolvedValueOnce({
      id: "s9",
      title: "Lipid yields",
      pinned_release_id: null,
      created_at: null,
      updated_at: null,
      messages: [
        {
          id: "u1",
          role: "user",
          content: "What lipid yield was reported?",
          created_at: null,
          release_id: null,
          citations: [],
          citation_validation_passed: true,
          unsupported_handles: [],
          latency_ms: 0,
        },
        {
          id: "m9",
          role: "assistant",
          content: "It reached 0.42 g/g [E0123456789].",
          created_at: null,
          release_id: "r1",
          release_label: "rla-corpus 1.1.0",
          citations: [
            {
              handle: "E0123456789",
              status: "valid",
              work_id: "w1",
              display_label: "Results",
              evidence_span_id: null,
            },
          ],
          citation_validation_passed: true,
          unsupported_handles: [],
          latency_ms: 900,
          sources: [LIPID],
        },
      ],
    });

    render(<AskClient />);
    await userEvent.click(await screen.findByRole("button", { name: /Sessions/ }));
    const drawer = screen.getByRole("dialog", { name: "Sessions" });
    await userEvent.click(await within(drawer).findByText("Lipid yields"));

    const sources = await screen.findByRole("list", { name: "Sources" });
    expect(within(sources).getByText(/Lipid production in an oleaginous yeast/)).toBeInTheDocument();
    expect(screen.queryByText(/Sources appear here/)).not.toBeInTheDocument();
    // Choosing a conversation closes the drawer.
    expect(screen.queryByRole("dialog", { name: "Sessions" })).not.toBeInTheDocument();
  });

  it("can show only the sources the answer cited", async () => {
    window.localStorage.removeItem("lab.ask.citedOnly");
    const uncited = { ...LIPID, handle: "Ebbbbbbbbbb", title: "A paper it did not cite" };
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([LIPID, uncited]));
      onEvent({ type: "token", data: "It reached 0.42 g/g [E0123456789]." });
      onEvent(doneEvent(["E0123456789"]));
    });
    render(<AskClient />);
    await askQuestion("yield?");

    const sources = await screen.findByRole("list", { name: "Sources" });
    expect(within(sources).getByText("A paper it did not cite")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cited 1" }));
    expect(within(sources).queryByText("A paper it did not cite")).not.toBeInTheDocument();
    expect(within(sources).getByText(/Lipid production in an oleaginous yeast/)).toBeInTheDocument();
    expect(window.localStorage.getItem("lab.ask.citedOnly")).toBe("1");
    await userEvent.click(screen.getByRole("button", { name: "All 2" }));
    expect(within(sources).getByText("A paper it did not cite")).toBeInTheDocument();
  });

  it("copies an answer with its references", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent(sourcesEvent([LIPID]));
      onEvent({ type: "token", data: "It reached 0.42 g/g [E0123456789]." });
      onEvent(doneEvent(["E0123456789"]));
    });
    render(<AskClient />);
    await askQuestion("yield?");
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    const copied = (writeText.mock.calls[0] as unknown as [string])[0];
    expect(copied).toContain("It reached 0.42 g/g [1].");
    expect(copied).toContain("1. Pazuki R, Ledesma-Amaro R, Smith J, Jones K.");
    expect(await screen.findByText("Copied, with references.")).toBeInTheDocument();
  });

  it("asks before deleting a session", async () => {
    vi.mocked(api.listSessions).mockResolvedValue({
      items: [
        { id: "s9", title: "Lipid yields", pinned_release_id: null, created_at: null, updated_at: null },
      ],
      next_cursor: null,
      total: 1,
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<AskClient />);
    await userEvent.click(await screen.findByRole("button", { name: /Sessions/ }));
    const remove = await screen.findByRole("button", { name: "Delete Lipid yields" });

    await userEvent.click(remove);
    expect(confirm).toHaveBeenLastCalledWith(expect.stringContaining("Delete “Lipid yields”?"));
    expect(api.deleteSession).not.toHaveBeenCalled();

    await userEvent.click(remove);
    await waitFor(() => expect(api.deleteSession).toHaveBeenCalledWith("s9"));
    confirm.mockRestore();
    vi.mocked(api.listSessions).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
  });

  it("keeps the sessions in a drawer that opens and closes", async () => {
    render(<AskClient />);
    const toggle = await screen.findByRole("button", { name: /Sessions/ });
    expect(screen.queryByRole("dialog", { name: "Sessions" })).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.getByRole("dialog", { name: "Sessions" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Sessions" })).not.toBeInTheDocument();
  });

  it("flags an answer that cited a handle the server never supplied", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({ type: "token", data: "An unsupported claim [Edeadbeef01]." });
      onEvent(doneEvent(["Edeadbeef01"], ["Edeadbeef01"]));
    });

    render(<AskClient />);
    await askQuestion("anything?");

    // A flagged answer, not a hidden one and not an error page.
    const warning = await screen.findByText(/did not supply/i);
    expect(warning.closest("[role=status]")).not.toBeNull();
    expect(screen.getByText(/An unsupported claim/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Source not supplied, Edeadbeef01" }),
    ).toHaveTextContent("?");
  });

  it("asks with Enter and keeps Shift+Enter for a new line", async () => {
    vi.mocked(api.ask).mockImplementation(async () => undefined);
    render(<AskClient />);
    const box = screen.getByLabelText(/question/i);
    await userEvent.type(box, "first line{Shift>}{Enter}{/Shift}second line");
    expect(box).toHaveValue("first line\nsecond line");
    await userEvent.type(box, "{Enter}");
    await waitFor(() =>
      expect(api.ask).toHaveBeenCalledWith(
        "s1",
        "first line\nsecond line",
        expect.any(Function),
        expect.anything(),
      ),
    );
  });

  it("shows a stream error as an alert", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({
        type: "error",
        data: { detail: "no release is published", error_class: "not_found" },
      });
    });
    render(<AskClient />);
    await askQuestion("yield?");
    expect(await screen.findByRole("alert")).toHaveTextContent("no release is published");
  });

  it("tells the member admins can read, and how much allowance is used", async () => {
    render(<AskClient />);
    expect(
      await screen.findByText(
        "Admins of this workspace can read your conversations. Every read is recorded.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText(/1,200 of 1,000,000/)).toBeInTheDocument();
  });

  it("stops offering to ask once the allowance is used up", async () => {
    vi.mocked(api.getAllowance).mockResolvedValueOnce({
      token_limit: 1000,
      used_tokens: 1000,
      remaining_tokens: 0,
      limit_reached: true,
      custom_limit: true,
    });
    render(<AskClient />);
    expect(await screen.findByText(/You have used your token allowance/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });
});
