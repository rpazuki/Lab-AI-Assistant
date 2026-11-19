import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/LoginForm";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

describe("LoginForm", () => {
  beforeEach(() => {
    push.mockReset();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  it("signs in and goes to Ask", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ workspaces: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/email/i), "s@lab.invalid");
    await userEvent.type(screen.getByLabelText(/password/i), "a-long-passphrase");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/ask"));
  });

  it("returns to the page the visitor was sent away from", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ workspaces: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<LoginForm next="/releases" />);
    await userEvent.type(screen.getByLabelText(/email/i), "s@lab.invalid");
    await userEvent.type(screen.getByLabelText(/password/i), "a-long-passphrase");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/releases"));
  });

  it("shows the failure without revealing which half was wrong", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "email or password is incorrect", error_class: "authentication_required" }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/email/i), "s@lab.invalid");
    await userEvent.type(screen.getByLabelText(/password/i), "wrong-passphrase");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("email or password is incorrect");
    expect(push).not.toHaveBeenCalled();
  });
});
