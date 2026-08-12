/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  // Servi en sous-chemin /demo par api/gex.py (STATIC dict), pas a la racine
  // -- sans basePath, les assets generes en /_next/... pointeraient a la
  // racine du site principal au lieu de /demo/_next/...
  basePath: '/demo',
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
}

export default nextConfig
