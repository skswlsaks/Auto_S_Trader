import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { StudyView } from "@/views/studies-view";

export const metadata = { title: "조건 실험 격자 · Model Arena" };
export default function Page() {
  return <Suspense fallback={<Loading />}><StudyView /></Suspense>;
}
