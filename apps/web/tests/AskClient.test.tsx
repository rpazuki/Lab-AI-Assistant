import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AskClient } from "@/components/ask/AskClient";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
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
    resolveCitation: vi.fn(async () => ({ resolved: true, nodes: [{ node_id: "p-1", text: "quote" }] })),
    sendFeedback: vi.fn(async () => ({ id: "f1" })),
    ask: vi.fn(),
  };
});

const api = await import("@/lib/api");

describe("AskClient", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.resetModules());

  it("shows sources before the answer text arrives", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({
        type: "sources",
        data: {
          release_id: "r1",
          classification: "evidence",
          sources: [
            {
              handle: "E0123456789",
              title: "Lipid production in an oleaginous yeast",
              year: 2025,
              source_label: "doi:10.1234/example",
              locator: "Results",
              display_restricted: false,
              snippet: "Strain YL-A reached a lipid yield of 0.42 g/g substrate.",
            },
          ],
          claims: [],
        },
      });
    });

    render(<AskClient />);
    await userEvent.type(screen.getByLabelText(/question/i), "What lipid yield was reported?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(await screen.findByText(/Lipid production in an oleaginous yeast/)).toBeInTheDocument();
    expect(screen.getByText("E0123456789")).toBeInTheDocument();
  });

  it("streams tokens into the answer and offers each citation", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({ type: "token", data: "Strain YL-A reached 0.42 g/g " });
      onEvent({ type: "token", data: "[E0123456789]." });
      onEvent({
        type: "done",
        data: {
          message_id: "m1",
          citations: [
            {
              handle: "E0123456789",
              status: "valid",
              work_id: "w1",
              display_label: "Results",
              evidence_span_id: "sp1",
            },
          ],
          unsupported_handles: [],
          citation_validation_passed: true,
          release_id: "r1",
          latency_ms: 42,
        },
      });
    });

    render(<AskClient />);
    await userEvent.type(screen.getByLabelText(/question/i), "yield?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(await screen.findByText(/Strain YL-A reached 0.42 g\/g/)).toBeInTheDocument();
    const citation = await screen.findByRole("button", { name: /E0123456789/ });
    await userEvent.click(citation);
    await waitFor(() => expect(api.resolveCitation).toHaveBeenCalledWith("m1", "E0123456789"));
    expect(await screen.findByText(/Evidence in context/)).toBeInTheDocument();
  });

  it("flags an answer that cited a handle the server never supplied", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({ type: "token", data: "An unsupported claim [Edeadbeef01]." });
      onEvent({
        type: "done",
        data: {
          message_id: "m2",
          citations: [
            { handle: "Edeadbeef01", status: "not_supplied", work_id: null, display_label: null, evidence_span_id: null },
          ],
          unsupported_handles: ["Edeadbeef01"],
          citation_validation_passed: false,
          release_id: "r1",
          latency_ms: 9,
        },
      });
    });

    render(<AskClient />);
    await userEvent.type(screen.getByLabelText(/question/i), "anything?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    // A flagged answer, not a hidden one and not an error page.
    const warning = await screen.findByRole("status");
    expect(warning).toHaveTextContent(/did not supply/i);
    expect(screen.getByText(/An unsupported claim/)).toBeInTheDocument();
  });

  it("shows a stream error as an alert", async () => {
    vi.mocked(api.ask).mockImplementation(async (_session, _question, onEvent) => {
      onEvent({ type: "error", data: { detail: "no release is published", error_class: "not_found" } });
    });
    render(<AskClient />);
    await userEvent.type(screen.getByLabelText(/question/i), "yield?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no release is published");
  });

  it("tells the member admins can read, and how much allowance is used", async () => {
    render(<AskClient />);
    expect(
      await screen.findByText("Admins of this workspace can read your conversations. Every read is recorded."),
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
