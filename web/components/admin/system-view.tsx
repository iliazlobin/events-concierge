"use client";

import { OperationsView, type OperationsViewProps } from "./operations-view";

export type SystemViewProps = OperationsViewProps;

export function SystemView(props: SystemViewProps): React.JSX.Element {
  return <OperationsView {...props} />;
}
