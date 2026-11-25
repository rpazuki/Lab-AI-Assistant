import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AbstractFallback } from "@/components/AbstractFallback";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getAbstractFallback: vi.fn(),
    setAbstractFallback: vi.fn(),
    buildAbstractDocuments: vi.fn(),
  };
});

const api = await import("@/lib/api");
const off = { enabled: false, set_at: null, set_by: null, partial: 0, eligible: 700 };

describe("Abstracts for papers without full text", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.getAbstractFallback).mockResolvedValue(off);
  });

  it("is off until a steward confirms, then builds every chunk", async () => {
    vi.mocked(api.setAbstractFallback).mockResolvedValue({ ...off, enabled: true, set_at: "2026-11-25T12:00:00+00:00" });
    vi.mocked(api.buildAbstractDocuments)
      .mockResolvedValueOnce({ built: 500, remaining: 200, enabled: 1 })
      .mockResolvedValueOnce({ built: 200, remaining: 0, enabled: 1 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<AbstractFallback />);

    expect(await screen.findByText(/700 more could/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Use abstracts in place of full texts that could not be fetched"));
    expect(confirm).toHaveBeenCalled();
    expect(api.setAbstractFallback).toHaveBeenCalledWith(true);
    expect(await screen.findByText(/700 papers now stand in as their abstract/)).toBeInTheDocument();
    expect(api.buildAbstractDocuments).toHaveBeenCalledTimes(2);
  });

  it("changes nothing when the steward does not confirm", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<AbstractFallback />);
    await user.click(
      await screen.findByLabelText("Use abstracts in place of full texts that could not be fetched"),
    );
    await waitFor(() => expect(api.setAbstractFallback).not.toHaveBeenCalled());
  });
});
