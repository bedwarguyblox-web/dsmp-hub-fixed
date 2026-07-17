import { useGetVouchLeaderboard, useGetScamVouchLeaderboard } from "@workspace/api-client-react";
import {
  Layout,
  PageHeader,
  TableWrap,
  Th,
  Td,
  LoadingRows,
  EmptyRow,
} from "@/components/layout";

function Board({
  title,
  data,
  loading,
  accent,
}: {
  title: string;
  data: { userId: string; total: number; rank: number }[] | undefined;
  loading: boolean;
  accent: string;
}) {
  return (
    <div className="flex-1 min-w-0">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">
        {title}
      </h2>
      <TableWrap>
        <thead>
          <tr>
            <Th>Rank</Th>
            <Th>User ID</Th>
            <Th>Total</Th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <LoadingRows cols={3} />
          ) : !data?.length ? (
            <EmptyRow cols={3} message="No data yet" />
          ) : (
            data.map((row) => (
              <tr key={row.userId} className="hover:bg-white/[0.02]">
                <Td>
                  <span className={`text-sm font-bold ${row.rank <= 3 ? accent : "text-gray-500"}`}>
                    #{row.rank}
                  </span>
                </Td>
                <Td className="font-mono text-xs">{row.userId}</Td>
                <Td>
                  <span className={`font-semibold tabular-nums ${accent}`}>{row.total}</span>
                </Td>
              </tr>
            ))
          )}
        </tbody>
      </TableWrap>
    </div>
  );
}

export default function Vouches() {
  const { data: vouches, isLoading: vLoading } = useGetVouchLeaderboard({ limit: 20 });
  const { data: scamVouches, isLoading: sLoading } = useGetScamVouchLeaderboard({ limit: 20 });

  return (
    <Layout>
      <div className="px-8 py-8 max-w-6xl">
        <PageHeader title="Vouches" subtitle="Leaderboards for vouches and scam vouches" />
        <div className="flex gap-8 flex-col md:flex-row">
          <Board
            title="Top Vouched Users"
            data={vouches}
            loading={vLoading}
            accent="text-sky-400"
          />
          <Board
            title="Top Scam Vouched Users"
            data={scamVouches}
            loading={sLoading}
            accent="text-red-400"
          />
        </div>
      </div>
    </Layout>
  );
}
