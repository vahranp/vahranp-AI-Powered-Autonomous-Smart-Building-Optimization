import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  title: string;
  description?: string;
  icon?: LucideIcon;
  action?: ReactNode;
  className?: string;
  contentClassName?: string;
  children: ReactNode;
}

export default function SectionCard({
  title,
  description,
  icon: Icon,
  action,
  className,
  contentClassName,
  children,
}: Props) {
  return (
    <Card className={className}>
      <CardHeader>
        <div className="flex items-center gap-2">
          {Icon && <Icon className="size-4 text-muted-foreground" />}
          <CardTitle className="text-sm">{title}</CardTitle>
        </div>
        {description && <CardDescription>{description}</CardDescription>}
        {action && <CardAction>{action}</CardAction>}
      </CardHeader>
      <CardContent className={contentClassName}>{children}</CardContent>
    </Card>
  );
}
