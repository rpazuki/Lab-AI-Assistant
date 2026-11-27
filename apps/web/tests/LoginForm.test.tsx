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

  it("turns a temporary password into a password of one's own", async () => {
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        json(
          {
            detail: "choose a password of your own to finish signing in",
            error_class: "authentication_required",
            context: { reason: "password_change_required" },
          },
          401,
        ),
      )
      .mockResolvedValueOnce(json({ user_id: "u9" }));
    render(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/email/i), "new@lab.invalid");
    await userEvent.type(screen.getByLabelText(/password/i), "h7kq-3mzt-x9pe-4wbn");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("status")).toHaveTextContent("works only once");
    await userEvent.type(screen.getByLabelText(/^New password/), "short");
    await userEvent.type(screen.getByLabelText("Repeat the new password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 12 characters");

    await userEvent.clear(screen.getByLabelText(/^New password/));
    await userEvent.clear(screen.getByLabelText("Repeat the new password"));
    await userEvent.type(screen.getByLabelText(/^New password/), "my-own-passphrase");
    await userEvent.type(screen.getByLabelText("Repeat the new password"), "my-own-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Set password and sign in" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/ask"));
    const [url, init] = vi.mocked(fetch).mock.calls[1]!;
    expect(String(url)).toMatch(/\/api\/auth\/first-sign-in$/);
    expect(JSON.parse(String(init?.body))).toEqual({
      email: "new@lab.invalid",
      temporary_password: "h7kq-3mzt-x9pe-4wbn",
      password: "my-own-passphrase",
    });
  });

  it("says when a temporary password has run out", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: "this temporary password has expired; ask an administrator for a new one",
          error_class: "authentication_required",
          context: { reason: "temporary_password_expired" },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/email/i), "late@lab.invalid");
    await userEvent.type(screen.getByLabelText(/password/i), "h7kq-3mzt-x9pe-4wbn");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("has expired");
    expect(screen.queryByRole("button", { name: "Set password and sign in" })).toBeNull();
  });

  it("posts, so a submit before the script loads never puts the password in the URL", () => {
    const { container } = render(<LoginForm />);
    expect(container.querySelector("form")).toHaveAttribute("method", "post");
  });
});
