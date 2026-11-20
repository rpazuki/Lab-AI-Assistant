import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HelpArticle, HelpIndex, rich } from "@/components/HelpClient";
import { PageHeader } from "@/components/ui/primitives";
import { HELP_TOPICS, helpFor, helpTopic, searchHelp } from "@/lib/help";
import { NAV } from "@/lib/navigation";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getMe: vi.fn() };
});

const api = await import("@/lib/api");

function member(capabilities: string[]) {
  return {
    user_id: "u1",
    email: "m@lab.invalid",
    full_name: null,
    workspace: { id: "w1", slug: "rlalab", name: "RLA Lab" },
    role: "researcher",
    capabilities,
  };
}

describe("the help content", () => {
  it("documents every page in the sidebar", () => {
    const pages = NAV.map((item) => item.href).filter((href) => href !== "/help");
    expect(pages.filter((href) => !helpFor(href))).toEqual([]);
  });

  it("opens each topic to the same members as its page", () => {
    for (const item of NAV.filter((entry) => entry.href !== "/help")) {
      expect(helpFor(item.href)?.anyOf).toEqual(item.anyOf);
    }
  });

  it("keeps every guide to one goal, titled how to", () => {
    for (const topic of HELP_TOPICS) {
      const titles = topic.sections.flatMap((section) => section.howTos.map((howTo) => howTo.title));
      expect(new Set(titles).size).toBe(titles.length);
      for (const section of topic.sections) {
        expect(section.about.length).toBeGreaterThanOrEqual(1);
        expect(section.about.length).toBeLessThanOrEqual(2);
        expect(section.howTos.length).toBeGreaterThan(0);
        for (const howTo of section.howTos) {
          expect(howTo.title).toMatch(/^How to [a-z]/);
          expect(howTo.steps.length).toBeGreaterThan(0);
        }
      }
    }
  });

  it("finds a guide by the words in its steps", () => {
    const [first] = searchHelp("invite link");
    expect(first?.howTo?.title).toBe("How to invite someone");
    expect(searchHelp("   ")).toEqual([]);
  });
});

describe("rich", () => {
  it("bolds screen names and italicises species, and nothing else", () => {
    render(<p>{rich("Select **Ask** about *Yarrowia* <b>x</b>")}</p>);
    expect(screen.getByText("Ask").tagName).toBe("STRONG");
    expect(screen.getByText("Yarrowia").tagName).toBe("EM");
    expect(screen.getByText(/<b>x<\/b>/)).toBeInTheDocument();
  });
});

describe("HelpIndex", () => {
  beforeEach(() => vi.mocked(api.getMe).mockResolvedValue(member(["ask", "view_analytics"])));

  it("lists every topic and marks those outside your role", async () => {
    render(<HelpIndex />);
    expect(screen.getAllByRole("link")).toHaveLength(HELP_TOPICS.length);
    const members = await screen.findByRole("link", { name: /^Members/ });
    expect(members).toHaveAttribute("href", "/help/members");
    expect(members).toHaveTextContent("not part of your role");
    expect(screen.getByRole("link", { name: /^Ask/ })).not.toHaveTextContent("not part of your role");
  });

  it("searches across every guide", async () => {
    render(<HelpIndex />);
    await userEvent.type(screen.getByRole("searchbox", { name: "Search the guides" }), "token limit");
    const results = screen.getByRole("region", { name: "Search results" });
    const link = within(results).getByRole("link", { name: /How to change a member's token limit/ });
    expect(link).toHaveAttribute("href", "/help/members#how-to-change-a-member-s-token-limit");
  });
});

describe("HelpArticle", () => {
  beforeEach(() => vi.mocked(api.getMe).mockResolvedValue(member(["ask"])));

  it("explains each section, then gives its numbered steps", async () => {
    render(<HelpArticle topic={helpTopic("ask")!} />);
    expect(screen.getByRole("heading", { level: 2, name: "Asking a question" })).toBeInTheDocument();
    const guide = screen.getByRole("article", { name: "How to ask a question" });
    expect(within(guide).getAllByRole("listitem")).toHaveLength(3);
    expect(within(guide).getByText(/^Note:/)).toBeInTheDocument();
    const onPage = screen.getByRole("navigation", { name: "On this page" });
    expect(within(onPage).getByRole("link", { name: "check a citation" })).toHaveAttribute(
      "href",
      "#how-to-check-a-citation",
    );
    expect(screen.getByRole("link", { name: "Open Ask" })).toHaveAttribute("href", "/ask");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says when the page is not part of your role", async () => {
    render(<HelpArticle topic={helpTopic("members")!} />);
    expect(await screen.findByRole("status")).toHaveTextContent("Your role does not include this page");
    expect(screen.getByRole("link", { name: "Members" })).toHaveAttribute("aria-current", "page");
  });
});

describe("PageHeader", () => {
  it("links to the page's guide", () => {
    render(<PageHeader pretitle="Research" title="Ask" help="/help/ask" />);
    expect(screen.getByRole("link", { name: "How-to guide" })).toHaveAttribute("href", "/help/ask");
  });
});
