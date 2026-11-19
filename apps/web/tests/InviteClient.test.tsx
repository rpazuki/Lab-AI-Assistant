import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InviteClient, tokenFromHash } from "@/components/InviteClient";
import { ApiError } from "@/lib/api";
import type { InvitationPreview } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, previewInvitation: vi.fn(), acceptInvitation: vi.fn(async () => undefined) };
});

const api = await import("@/lib/api");

const TOKEN = "a-token-long-enough-to-pass";

const PREVIEW: InvitationPreview = {
  email: "new@lab.invalid",
  role: "data_steward",
  workspace: { slug: "rlalab", name: "RLA Lab" },
  expires_at: "2026-11-25T10:00:00Z",
  has_account: false,
};

function visit(hash: string) {
  window.history.replaceState(null, "", `/assist/invite${hash}`);
}

describe("tokenFromHash", () => {
  it("reads the token from the fragment", () => {
    expect(tokenFromHash(`#token=${TOKEN}`)).toBe(TOKEN);
  });

  it.each(["", "#", "#token=", "#token=short", "#other=value"])("finds none in %j", (hash) => {
    expect(tokenFromHash(hash)).toBeNull();
  });
});

describe("InviteClient", () => {
  let assign: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    assign = vi.fn();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("says an invitation without a token is not valid", async () => {
    visit("");
    render(<InviteClient />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This invitation is not valid");
    expect(api.previewInvitation).not.toHaveBeenCalled();
  });

  it("says a refused token is not valid", async () => {
    visit(`#token=${TOKEN}`);
    vi.mocked(api.previewInvitation).mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Forbidden",
        status: 403,
        detail: "this invitation is not valid",
        error_class: "policy_blocked",
      }),
    );
    render(<InviteClient />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Ask an administrator for a new link");
  });

  it("takes the token out of the address bar once read", async () => {
    visit(`#token=${TOKEN}`);
    vi.mocked(api.previewInvitation).mockResolvedValue(PREVIEW);
    render(<InviteClient />);
    await screen.findByText(/You are invited to/);
    expect(api.previewInvitation).toHaveBeenCalledWith(TOKEN);
    expect(window.location.hash).toBe("");
  });

  it("creates an account and arrives signed in", async () => {
    vi.stubGlobal("location", { ...window.location, hash: `#token=${TOKEN}`, assign });
    vi.mocked(api.previewInvitation).mockResolvedValue(PREVIEW);
    render(<InviteClient />);
    expect(await screen.findByText("data steward")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/^Password/), "a-long-enough-passphrase");
    await userEvent.type(screen.getByLabelText("Repeat the password"), "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Create account and join" }));

    expect(api.acceptInvitation).toHaveBeenCalledWith({
      token: TOKEN,
      password: "a-long-enough-passphrase",
      full_name: undefined,
    });
    await waitFor(() => expect(assign).toHaveBeenCalledWith(expect.stringMatching(/\/ask$/)));
  });

  it("checks the password before sending it", async () => {
    visit(`#token=${TOKEN}`);
    vi.mocked(api.previewInvitation).mockResolvedValue(PREVIEW);
    render(<InviteClient />);
    await screen.findByText(/You are invited to/);
    await userEvent.type(screen.getByLabelText(/^Password/), "short");
    await userEvent.type(screen.getByLabelText("Repeat the password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Create account and join" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 12 characters");
    expect(api.acceptInvitation).not.toHaveBeenCalled();
  });

  it("asks an existing account for its own password", async () => {
    visit(`#token=${TOKEN}`);
    vi.mocked(api.previewInvitation).mockResolvedValue({ ...PREVIEW, has_account: true });
    render(<InviteClient />);
    expect(await screen.findByLabelText("Your existing password")).toBeInTheDocument();
    expect(screen.queryByLabelText("Repeat the password")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Join" })).toBeInTheDocument();
  });
});
