import { useGetStats, useGetRecentActivity } from "@workspace/api-client-react";
import {
  Layout,
  PageHeader,
  StatCard,
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

function actionColor(t: string) {
  if (t.includes("strike")) return "text-red-400";
  if (t.includes("vouch")) return "text-indigo-400";
  if (t.includes("builder")) return "text-amber-400";
  return "text-gray-300";
}

export default function Overview() {
  const { data: stats, isLoading: statsLoading } = useGetStats();
  const { data: activity, isLoading: actLoading } = useGetRecentActivity({ limit: 10 });

  return (
    <Layout>
      <div className="px-8 py-8 max-w-6xl">
        <PageHeader title="Overview" subtitle="Live stats from the bot database" />

        {/* Stat grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
          {statsLoading ? (
            [...Array(7)].map((_, i) => (
              <div key={i} className="bg-[#161b22] border border-white/10 rounded-lg px-5 py-4 animate-pulse h-24" />
            ))
          ) : (
            <>
              <StatCard label="Total Vouches" value={stats?.totalVouches ?? 0} />
              <StatCard label="Scam Vouches" value={stats?.totalScamVouches ?? 0} />
              <StatCard label="Total Strikes" value={stats?.totalStrikes ?? 0} />
              <StatCard label="Builder Cases" value={stats?.totalBuilderCases ?? 0} />
              <StatCard label="Builder Payments" value={stats?.totalPayments ?? 0} />
              <StatCard label="Active Timers" value={stats?.activeTimers ?? 0} />
              <StatCard
                label="Actions (24h)"
                value={stats?.recentActions ?? 0}
                sub="Staff actions in last 24 hours"
              />
            </>
          )}
        </div>

        {/* Recent activity */}
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
          Recent Activity
        </h2>
        <TableWrap>
          <thead>
            <tr>
              <Th>Action</Th>
              <Th>Actor ID</Th>
              <Th>Target ID</Th>
              <Th>Details</Th>
              <Th>Time</Th>
            </tr>
          </thead>
          <tbody>
            {actLoading ? (
              <LoadingRows cols={5} />
            ) : !activity?.length ? (
              <EmptyRow cols={5} message="No activity recorded yet" />
            ) : (
              activity.map((e) => (
                <tr key={e.id} className="hover:bg-white/[0.02]">
                  <Td>
                    <span className={actionColor(e.actionType)}>{e.actionType}</span>
                  </Td>
                  <Td className="font-mono text-xs">{e.actorId}</Td>
                  <Td className="font-mono text-xs">{e.targetId ?? "—"}</Td>
                  <Td className="max-w-xs truncate text-gray-400">{e.details ?? "—"}</Td>
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
