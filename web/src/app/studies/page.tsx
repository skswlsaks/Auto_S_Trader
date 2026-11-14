import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { StudiesView } from "@/views/studies-view";

export const metadata = { title: "조건 실험 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><StudiesView /></Suspense>;
}
