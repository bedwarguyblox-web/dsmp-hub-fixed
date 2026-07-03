import { useGetRecentActivity } from "@workspace/api-client-react";
import {
  Layout,
  PageHeader,
  TableWrap,
  Th,
  Td,
  LoadingRows,
  EmptyRow,
} from "@/components/layout";

function fmt(ts: string) {
  try {
    return new Date(ts.replace(" ", "T") + "Z").toLocaleString();
  } catch {
    return ts;
  }
}

const ACTION_COLORS: Record<string, string> = {
  strike_add: "text-red-400",
  strike_remove: "text-emerald-400",
  strike_reset: "text-orange-400",
  vouch_add: "text-indigo-400",
  scam_vouch_add: "text-pink-400",
  builder_case_create: "text-amber-400",
  builder_case_start: "text-amber-300",
  builder_case_complete: "text-emerald-400",
  builder_case_dispute: "text-red-400",
  builder_payment: "text-amber-400",
  serverify: "text-blue-400",
};

function actionColor(t: string) {
  return ACTION_COLORS[t] ?? "text-gray-300";
}

export default function Activity() {
  const { data, isLoading } = useGetRecentActivity({ limit: 100 });

  return (
    <Layout>
      <div className="px-8 py-8 max-w-5xl">
        <PageHeader
          title="Activity Log"
          subtitle="Last 100 staff actions — refreshes every 15 seconds"
        />
        <TableWrap>
          <thead>
            <tr>
              <Th>#</Th>
              <Th>Action</Th>
              <Th>Actor ID</Th>
              <Th>Target ID</Th>
              <Th>Details</Th>
              <Th>Time</Th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingRows cols={6} />
            ) : !data?.length ? (
              <EmptyRow cols={6} message="No staff actions recorded yet" />
            ) : (
              data.map((e) => (
                <tr key={e.id} className="hover:bg-white/[0.02]">
                  <Td className="text-gray-600 text-xs tabular-nums">{e.id}</Td>
                  <Td>
                    <span className={`text-sm font-medium ${actionColor(e.actionType)}`}>
                      {e.actionType.replace(/_/g, " ")}
                    </span>
                  </Td>
                  <Td className="font-mono text-xs">{e.actorId}</Td>
                  <Td className="font-mono text-xs">{e.targetId ?? "—"}</Td>
                  <Td className="max-w-xs">
                    <span className="text-gray-400 text-xs">{e.details ?? "—"}</span>
                  </Td>
                  <Td className="text-gray-500 text-xs whitespace-nowrap">{fmt(e.timestamp)}</Td>
                </tr>
              ))
            )}
          </tbody>
        </TableWrap>
      </div>
    </Layout>
  );
}
