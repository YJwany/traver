import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ExportPanel } from "./export-panel";

/**
 * The export panel: the note, the buttons, and the one claim it must not invent.
 *
 * The panel exists because a downloaded file leaves the application, where the sources and
 * the freshness panel normally explain every number. So the tests are about what the panel
 * *says*, with one exception that is a real defect class in this codebase: an absent
 * `age_unknown_facts` is not an empty one. An API that predates the field sends nothing,
 * and rendering「全部可验证」from that would assert a coverage check that never ran — the
 * same manufactured verification the freshness panel was fixed for.
 *
 * The download itself is mocked. `downloadExport` builds a blob and clicks an anchor, which
 * jsdom cannot meaningfully perform; what matters here is that the button reaches it with the
 * right trip and format, and that a failure surfaces instead of doing nothing.
 */

const downloadExport = vi.fn();

vi.mock("../../lib/export", () => ({
  EXPORT_PATHS: { md: "export.md", ics: "export.ics" },
  downloadExport: (...args: unknown[]) => downloadExport(...args),
}));

function trip(overrides: Record<string, unknown> = {}) {
  return {
    id: "trip-1",
    profile: { destination: "杭州" },
    itinerary: { days: [{ items: [{}, {}] }, { items: [{}] }] },
    preview: { source_references: [{}, {}, {}], age_unknown_facts: [] },
    ...overrides,
  };
}

beforeEach(() => {
  downloadExport.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ExportPanel", () => {
  it("states that the export is a snapshot rather than a live view", () => {
    render(<ExportPanel trip={trip()} />);

    expect(screen.getByText(/不会自动更新/)).toBeTruthy();
    // The reason it is a snapshot, not just the fact: the file carries both times. Matched
    // among all of them because the note appendix mentions the same pair of times.
    expect(screen.getAllByText(/抓取时间/).length).toBeGreaterThanOrEqual(1);
  });

  it("says the traveller's own material goes into the file too", () => {
    // The appendix exists because a saved post can be deleted while a POI cannot. A reader who
    // does not know it is in the file has no reason to keep the file.
    render(<ExportPanel trip={trip()} />);

    expect(screen.getByText(/你自己交进来的内容/)).toBeTruthy();
    expect(screen.getByText(/可能明天就被删掉/)).toBeTruthy();
  });

  it("warns that calendar times are wall-clock and will be shown in the device's zone", () => {
    // The specific trap: floating time is encoded honestly, but a client renders it in its
    // own zone, so a 东京 plan on a 伦敦 phone shows shifted hours while looking unchanged.
    render(<ExportPanel trip={trip()} />);

    expect(screen.getByText(/目的地当地时间/)).toBeTruthy();
    expect(screen.getByText(/按设备时区显示/)).toBeTruthy();
    expect(screen.getByText(/暂定/)).toBeTruthy();
  });

  it("counts the itinerary items and the sources it is exporting", () => {
    render(<ExportPanel trip={trip()} />);
    expect(screen.getByText(/3 个行程项 · 3 条来源/)).toBeTruthy();
  });

  it("does not claim full verification when the coverage field is absent", () => {
    // An older API: the key is missing entirely, so nothing was checked as far as this
    // client can tell. Absent must not be read as empty.
    const preview = { source_references: [{}, {}] };
    render(<ExportPanel trip={trip({ preview })} />);

    // It must say the coverage is unknown rather than staying silent, because silence here
    // reads as "fine".
    expect(screen.getByText(/没有时效性核查记录/)).toBeTruthy();
    // And it must not fall into the other branch — the one that names a count of
    // unverifiable sources, which would mean it had treated absence as a short list.
    expect(screen.queryByText(/条来源没有提供数据自身的时间/)).toBeNull();
  });

  it("reports how many sources cannot have their age judged", () => {
    const preview = { source_references: [{}, {}, {}], age_unknown_facts: [{}, {}] };
    render(<ExportPanel trip={trip({ preview })} />);

    expect(screen.getByText(/其中 2 条来源没有提供数据自身的时间/)).toBeTruthy();
    // The distinction the wording has to preserve: unknown is neither fresh nor stale.
    expect(screen.getByText(/只等于无法判断/)).toBeTruthy();
  });

  it("stays quiet about coverage when every source states its time", () => {
    render(<ExportPanel trip={trip()} />);
    expect(screen.queryByText(/其中 \d+ 条来源没有提供/)).toBeNull();
    expect(screen.queryByText(/没有时效性核查记录/)).toBeNull();
  });

  it("downloads the Markdown export for this trip when asked", async () => {
    downloadExport.mockResolvedValue("杭州-2026-10-03.md");
    render(<ExportPanel trip={trip()} />);

    fireEvent.click(screen.getByRole("button", { name: "下载 Markdown" }));

    await waitFor(() => expect(downloadExport).toHaveBeenCalledWith("trip-1", "md", "杭州"));
    expect(await screen.findByText(/已保存 杭州-2026-10-03\.md/)).toBeTruthy();
  });

  it("offers the calendar in the other format", async () => {
    downloadExport.mockResolvedValue("杭州-2026-10-03.ics");
    render(<ExportPanel trip={trip()} />);

    fireEvent.click(screen.getByRole("button", { name: "下载日历" }));

    await waitFor(() => expect(downloadExport).toHaveBeenCalledWith("trip-1", "ics", "杭州"));
  });

  it("shows a failure instead of appearing to have done nothing", async () => {
    // A silent no-op is the worst outcome: the user believes a file exists.
    downloadExport.mockRejectedValue(new Error("导出失败（HTTP 404）"));
    render(<ExportPanel trip={trip()} />);

    fireEvent.click(screen.getByRole("button", { name: "下载 Markdown" }));

    expect(await screen.findByText("导出失败（HTTP 404）")).toBeTruthy();
    expect(screen.queryByText(/已保存/)).toBeNull();
  });

  it("disables both buttons while a download is in flight", async () => {
    let release: (value: string) => void = () => {};
    downloadExport.mockReturnValue(new Promise<string>((resolve) => (release = resolve)));
    render(<ExportPanel trip={trip()} />);

    fireEvent.click(screen.getByRole("button", { name: "下载 Markdown" }));

    const calendar = screen.getByRole("button", { name: "下载日历" }) as HTMLButtonElement;
    expect(calendar.disabled).toBe(true);
    expect(screen.getByRole("button", { name: "正在导出…" })).toBeTruthy();

    release("x.md");
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "下载日历" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });

  it("tolerates a trip whose preview has not been built yet", () => {
    // A freshly created trip has no preview. The panel must still render, and must not
    // describe coverage it cannot see.
    render(<ExportPanel trip={trip({ preview: undefined })} />);
    expect(screen.getByRole("button", { name: "下载 Markdown" })).toBeTruthy();
  });
});
