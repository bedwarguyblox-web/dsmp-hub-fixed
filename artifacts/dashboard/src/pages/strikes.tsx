import { useGetRecentStrikes } from "@workspace/api-client-react";
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

function fmt(ts: string) {
  try {
    return new Date(ts.replace(" ", "T") + "Z").toLocaleString();
  } catch {
    return ts;
  }
}

export default function Strikes() {
  const { data, isLoading } = useGetRecentStrikes({ limit: 50 });

  return (
    <Layout>
      <div className="px-8 py-8 max-w-5xl">
        <PageHeader title="Strike History" subtitle="Most recent 50 strike events across all members" />
        <TableWrap>
          <thead>
            <tr>
              <Th>#</Th>
              <Th>Action</Th>
              <Th>User ID</Th>
              <Th>Moderator ID</Th>
              <Th>Reason</Th>
              <Th>Time</Th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingRows cols={6} />
            ) : !data?.length ? (
              <EmptyRow cols={6} message="No strikes recorded yet" />
            ) : (
              data.map((s) => (
                <tr key={s.id} className="hover:bg-white/[0.02]">
                  <Td className="text-gray-500 text-xs tabular-nums">{s.id}</Td>
                  <Td>
                    <Badge variant={s.action === "add" ? "red" : "green"}>
                      {s.action === "add" ? "Strike Added" : "Strike Removed"}
                    </Badge>
                  </Td>
                  <Td className="font-mono text-xs">{s.userId}</Td>
                  <Td className="font-mono text-xs">{s.moderatorId}</Td>
                  <Td className="max-w-xs">
                    <span className="text-gray-300 text-sm">{s.reason}</span>
                  </Td>
                  <Td className="text-gray-500 text-xs whitespace-nowrap">{fmt(s.timestamp)}</Td>
                </tr>
              ))
            )}
          </tbody>
        </TableWrap>
      </div>
    </Layout>
  );
}
