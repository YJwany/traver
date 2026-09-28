import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { Disagreements } from "./disagreements";

/**
 * Where two sources contradicted each other.
 *
 * The merge records disagreements, but recording is only half the fix — a stored conflict
 * that never reaches the screen leaves the page exactly as misleading as before, still
 * showing one number beside two source names. These tests pin that the conflict is shown,
 * that every claim survives to the screen, and that the retained value is named as the one
 * being displayed.
 *
 * The distinction that matters most: an empty panel must mean "the sources agreed", not
 * "a disagreement was recorded and then dropped". So the component has to render nothing
 * when there is nothing to say, and something specific when there is.
 */

function entityWith(disagreements: unknown[]) {
  return { id: "attraction-1", canonical_name: "西湖", disagreements };
}

const TICKET_DISAGREEMENT = {
  field: "ticket_price",
  chosen_source: "amap",
  chosen_value_text: "¥80",
  chosen_reason: "取观测时间最新的一条（amap）：这条事实的时效性优先。",
  claims: [
    {
      source: "openstreetmap",
      source_id: "osm-1",
      value_text: "¥60",
      is_estimate: false,
      observed_at: "2026-09-28T09:00:00Z",
      fetched_at: "2026-09-28T09:00:01Z",
    },
    {
      source: "amap",
      source_id: "amap-1",
      value_text: "¥80",
      is_estimate: false,
      observed_at: "2026-09-28T10:00:00Z",
      fetched_at: "2026-09-28T10:00:01Z",
    },
  ],
};

describe("Disagreements", () => {
  it("renders nothing when the sources agreed", () => {
    // A panel that always appeared would train the reader to ignore it, which costs the
    // one signal that says "verify this yourself".
    const { container } = render(<Disagreements entity={entityWith([])} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the field is absent from an older API response", () => {
    // The settings page made this mistake once by making a new field required: an older
    // server must not crash the workspace.
    const { container } = render(<Disagreements entity={{ id: "a", canonical_name: "西湖" }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows every claim, not only the losing one", () => {
    const { container } = render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    const claims = container.querySelector(".disagreement-claims")!;

    // Both values must be visible: "two sources, ¥60 and ¥80" is different information
    // from "two sources disagreed", and a reader needs the actual numbers.
    expect(within(claims as HTMLElement).getByText(/¥60/)).toBeInTheDocument();
    expect(within(claims as HTMLElement).getByText(/¥80/)).toBeInTheDocument();
    expect(within(claims as HTMLElement).getByText("openstreetmap")).toBeInTheDocument();
    expect(within(claims as HTMLElement).getByText("amap")).toBeInTheDocument();
  });

  it("names the value that is being displayed and where it came from", () => {
    // The number in the card above is what a traveller acts on, so the panel has to make
    // clear which claim that number is — otherwise the reader cannot tell which source
    // the screen is following.
    render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);

    const body = screen.getByText(/当前显示的是/).closest("div")!;
    expect(within(body).getByText("¥80")).toBeInTheDocument();
    expect(within(body).getByText(/amap/)).toBeInTheDocument();
  });

  it("states the rule that picked the retained value", () => {
    // An unexplained choice is indistinguishable from an arbitrary one.
    render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    expect(screen.getByText(/时效性优先/)).toBeInTheDocument();
  });

  it("labels the fields in words a traveller reads", () => {
    render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    // Not "ticket_price": the panel is read by someone planning a day out.
    expect(screen.getByText("门票")).toBeInTheDocument();
    expect(screen.queryByText("ticket_price")).toBeNull();
  });

  it("falls back to the raw field name rather than showing nothing", () => {
    // A new comparable field must appear as something rather than as a blank label,
    // because a blank label hides exactly the change someone needs to notice.
    render(
      <Disagreements
        entity={entityWith([{ ...TICKET_DISAGREEMENT, field: "some_new_field" }])}
      />,
    );
    expect(screen.getByText("some_new_field")).toBeInTheDocument();
  });

  it("reports every disagreeing field, not just the first", () => {
    render(
      <Disagreements
        entity={entityWith([
          TICKET_DISAGREEMENT,
          { ...TICKET_DISAGREEMENT, field: "rating", chosen_value_text: "3.0 分" },
        ])}
      />,
    );
    expect(screen.getByText(/2 个字段/)).toBeInTheDocument();
    expect(screen.getByText("门票")).toBeInTheDocument();
    expect(screen.getByText("评分")).toBeInTheDocument();
  });

  it("shows when each claim was observed", () => {
    // The difference between a two-year-old and a two-hour-old claim is the whole reason
    // recency was chosen as the tie-break, so the dates cannot be hidden. Scoped to the
    // claim list because the reason text also mentions 观测.
    const { container } = render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    const claims = container.querySelector(".disagreement-claims") as HTMLElement;

    expect(within(claims).getAllByText(/观测/).length).toBe(2);
    // And the actual times, not a placeholder.
    expect(within(claims).getByText(/2026-09-28 09:00/)).toBeInTheDocument();
    expect(within(claims).getByText(/2026-09-28 10:00/)).toBeInTheDocument();
  });

  it("does not present an empty panel as verification", () => {
    // The wording has to say that silence is not proof, because a reader who takes
    // "no warning" for "confirmed" would trust the number more than it deserves.
    render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    expect(screen.getByText(/不代表数据已被核实/)).toBeInTheDocument();
  });

  it("does not claim the differences were averaged away", () => {
    render(<Disagreements entity={entityWith([TICKET_DISAGREEMENT])} />);
    expect(screen.getByText(/没有被自动抹平/)).toBeInTheDocument();
  });
});
