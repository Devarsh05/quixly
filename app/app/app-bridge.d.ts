import type React from "react";

// App Bridge renders <s-app-nav> as a custom element. @shopify/polaris-types augments the JSX
// namespaces for the other s-* elements (s-link, s-button, …) but not s-app-nav, so
// `tsc --noEmit` errors TS2339 on it in app/routes/app.tsx. Register it the same way
// polaris-types registers its elements — on the `react` module and the global JSX namespace.
type SAppNavProps = React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;

declare global {
  namespace JSX {
    interface IntrinsicElements {
      "s-app-nav": SAppNavProps;
    }
  }
}

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "s-app-nav": SAppNavProps;
    }
  }
}
