import type { MetadataRoute } from "next";
import { SITE_LINKS } from "@/lib/content";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
    },
    sitemap: `${SITE_LINKS.homepage}/sitemap.xml`,
  };
}
