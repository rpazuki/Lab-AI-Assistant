import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MirrorAccessClient } from "@/components/MirrorAccessClient";
import { ApiError } from "@/lib/api";
import type { MirrorAccess } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getMirrorAccess: vi.fn(), saveMirrorAccess: vi.fn() };
});

const api = await import("@/lib/api");

const view = (overrides: Partial<MirrorAccess> = {}): MirrorAccess => ({
  scopes: [
    { scope_id: "s-1", name: "Yarrowia lipids", papers: 120, withheld: false },
    { scope_id: "s-2", name: "Confidential strains", papers: 8, withheld: false },
  ],
  releases: [
    {
      release_id: "r-2",
      name: "corpus",
      semantic_version: "2026.11.27",
      status: "published",
      published_at: "2026-11-27T10:00:00Z",
      withheld: false,
      publisher_choice: true,
      serving: true,
    },
    {
      release_id: "r-1",
      name: "corpus",
      semantic_version: "2026.11.01",
      status: "superseded",
      published_at: "2026-11-01T10:00:00Z",
      withheld: false,
      publisher_choice: false,
      serving: false,
    },
  ],
  serving_release_id: "r-2",
  withheld_papers: 0,
  set_at: null,
  set_by: null,
  ...overrides,
});

describe("Mirror access", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists the scopes and releases, all served, and saves nothing until asked", async () => {
    vi.mocked(api.getMirrorAccess).mockResolvedValue(view());
    render(<MirrorAccessClient />);
    expect(await screen.findByRole("checkbox", { name: "Serve Yarrowia lipids" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Serve corpus 2026.11.27" })).toBeChecked();
    expect(screen.getByText("Answering from it now. The publisher's choice")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("sends what is unticked as the removed list", async () => {
    vi.mocked(api.getMirrorAccess).mockResolvedValue(view());
    vi.mocked(api.saveMirrorAccess).mockResolvedValue(
      view({ withheld_papers: 8, set_at: "2026-11-27T12:00:00Z" }),
    );
    const user = userEvent.setup();
    render(<MirrorAccessClient />);
    await user.click(await screen.findByRole("checkbox", { name: "Serve Confidential strains" }));
    await user.click(screen.getByRole("checkbox", { name: "Serve corpus 2026.11.27" }));
    // The publisher's choice unticked: the page says what will be served instead.
    expect(screen.getByText(/answers from the newest ticked release instead/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(api.saveMirrorAccess).toHaveBeenCalledWith({
      withheld_scope_ids: ["s-2"],
      withheld_release_ids: ["r-2"],
    });
    expect(await screen.findByText(/follow it from now on/)).toBeInTheDocument();
  });

  it("warns before saving a choice that serves no release at all", async () => {
    vi.mocked(api.getMirrorAccess).mockResolvedValue(view());
    const user = userEvent.setup();
    render(<MirrorAccessClient />);
    await user.click(await screen.findByRole("checkbox", { name: "Serve corpus 2026.11.27" }));
    await user.click(screen.getByRole("checkbox", { name: "Serve corpus 2026.11.01" }));
    expect(screen.getByText(/this mirror serves nothing/)).toBeInTheDocument();
  });

  it("puts the boxes back with Undo changes", async () => {
    vi.mocked(api.getMirrorAccess).mockResolvedValue(view());
    const user = userEvent.setup();
    render(<MirrorAccessClient />);
    const box = await screen.findByRole("checkbox", { name: "Serve Yarrowia lipids" });
    await user.click(box);
    expect(box).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Undo changes" }));
    expect(box).toBeChecked();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("on the publisher, says where the equivalent controls are", async () => {
    vi.mocked(api.getMirrorAccess).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Not Implemented",
        status: 501,
        detail: "only a mirror removes scopes or releases",
        error_class: "policy_blocked",
      } as never),
    );
    render(<MirrorAccessClient />);
    await waitFor(() =>
      expect(screen.getByText(/This installation is the publisher/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
