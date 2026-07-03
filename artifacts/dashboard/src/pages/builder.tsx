import { useGetBuilderCases, useGetBuilderPayments } from "@workspace/api-client-react";
import {
  Layout,
  PageHeader,
  TableWrap,
  Th,
  Td,
  LoadingRows,
  EmptyRow,
  Badge,
} from "@/components/layout";

function fmt(ts: string | null) {
  if (!ts) return "—";
  try {
    return new Date(ts.replace(" ", "T") + "Z").toLocaleString();
  } catch {
    return ts;
  }
}

function statusBadge(status: string) {
  const map: Record<string, "yellow" | "green" | "gray" | "red" | "blue"> = {
    pending_confirmation: "yellow",
    active: "green",
    completed: "gray",
    disputed: "red",
    cancelled: "gray",
  };
  return (
    <Badge variant={map[status] ?? "blue"}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

export default function Builder() {
  const { data: cases, isLoading: cLoading } = useGetBuilderCases();
  const { data: payments, isLoading: pLoading } = useGetBuilderPayments({ limit: 30 });

  return (
    <Layout>
      <div className="px-8 py-8 max-w-6xl space-y-10">
        <PageHeader title="Builder Protection" subtitle="Cases and payment records" />

        {/* Cases */}
        <div>
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
            All Cases
          </h2>
          <TableWrap>
            <thead>
              <tr>
                <Th>Case ID</Th>
                <Th>Status</Th>
                <Th>Builder ID</Th>
                <Th>Customer ID</Th>
                <Th>IGN</Th>
                <Th>Amount</Th>
                <Th>Start</Th>
                <Th>End</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {cLoading ? (
                <LoadingRows cols={9} />
              ) : !cases?.length ? (
                <EmptyRow cols={9} message="No builder cases yet" />
              ) : (
                cases.map((c) => (
                  <tr key={c.caseId} className="hover:bg-white/[0.02]">
                    <Td className="font-mono text-xs">{c.caseId}</Td>
                    <Td>{statusBadge(c.status)}</Td>
                    <Td className="font-mono text-xs">{c.builderId}</Td>
                    <Td className="font-mono text-xs">{c.customerId}</Td>
                    <Td>{c.ign}</Td>
                    <Td className="text-amber-400 font-medium">{c.amount}</Td>
                    <Td className="text-xs text-gray-500 whitespace-nowrap">{fmt(c.startTime ?? null)}</Td>
                    <Td className="text-xs text-gray-500 whitespace-nowrap">{fmt(c.endTime ?? null)}</Td>
                    <Td className="text-xs text-gray-500 whitespace-nowrap">{fmt(c.createdAt)}</Td>
                  </tr>
                ))
              )}
            </tbody>
          </TableWrap>
        </div>

        {/* Payments */}
        <div>
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
            Recent Payments
          </h2>
          <TableWrap>
            <thead>
              <tr>
                <Th>Payment ID</Th>
                <Th>Staff ID</Th>
                <Th>IGN</Th>
                <Th>Amount</Th>
                <Th>Time</Th>
              </tr>
            </thead>
            <tbody>
              {pLoading ? (
                <LoadingRows cols={5} />
              ) : !payments?.length ? (
                <EmptyRow cols={5} message="No payments recorded yet" />
              ) : (
                payments.map((p) => (
                  <tr key={p.id} className="hover:bg-white/[0.02]">
                    <Td className="font-mono text-xs">{p.paymentId}</Td>
                    <Td className="font-mono text-xs">{p.staffId}</Td>
                    <Td>{p.ign}</Td>
                    <Td className="text-amber-400 font-medium">{p.amount}</Td>
                    <Td className="text-xs text-gray-500 whitespace-nowrap">
                      {fmt(p.timestamp)}
                    </Td>
                  </tr>
                ))
              )}
            </tbody>
          </TableWrap>
        </div>
      </div>
    </Layout>
  );
}
