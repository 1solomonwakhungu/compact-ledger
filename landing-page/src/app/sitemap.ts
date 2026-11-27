import type { MetadataRoute } from "next";
import { SITE_LINKS } from "@/lib/content";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    {
      url: SITE_LINKS.homepage,
      lastModified: new Date(),
      changeFrequency: "weekly",
      priority: 1,
    },
  ];
}
