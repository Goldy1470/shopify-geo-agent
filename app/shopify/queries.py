"""GraphQL Admin API queries used by the catalog sync.

Fields verified against shopify.dev/docs/api/admin-graphql (2026-10):
- productUpdate (write_products) for field writes; variants need
  productVariantsBulkUpdate, so variants are mirror-only for now.
- metafieldsSet accepts max 25 metafields/call, atomic; JSON values cap at
  128KB (2026-04+). Type is "json" (old "json_string" is legacy).
- Cost-based throttling reads extensions.cost.throttleStatus.
"""

PRODUCT_FIELDS_FRAGMENT = """
  fragment ProductFields on Product {
    id
    title
    handle
    descriptionHtml
    vendor
    productType
    tags
    status
    createdAt
    updatedAt
    seo { title description }
    category { id name }
    priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
    images(first: 100) { nodes { url altText position } }
    video: media(first: 20, query: "media_type:video") { nodes { ... on Video { id altText } ... on ExternalVideo { id } } }
    metafields(first: 50) { nodes { namespace key type value } }
    variants(first: 100) { nodes {
      id title sku barcode position price { amount } currentlyNotAvailable
    } }
  }
"""

PRODUCTS_PAGE_QUERY = """
  query Products($cursor: String, $limit: Int!) {
    products(first: $limit, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { ...ProductFields }
    }
  }
""" + PRODUCT_FIELDS_FRAGMENT

SINGLE_PRODUCT_QUERY = """
  query Product($id: ID!) {
    product(id: $id) { ...ProductFields }
  }
""" + PRODUCT_FIELDS_FRAGMENT

# FAQ lives in a geo_agent.faq_json metafield (type json), rendered by a theme
# block; sizes stay far below the 128KB JSON write cap.
PRODUCT_UPDATE_MUTATION = """
  mutation ProductUpdate($input: ProductUpdateInput!) {
    productUpdate(product: $input) {
      product { id title descriptionHtml }
      userErrors { field message }
    }
  }
"""

METAFIELDS_SET_MUTATION = """
  mutation MetafieldsSet($metafields: [MetafieldsSetInput!]!) {
    metafieldsSet(metafields: $metafields) {
      metafields { id key namespace value }
      userErrors { field message code }
    }
  }
"""

BULK_RUN_MUTATION = """
  mutation BulkOperationRunQuery($query: String!) {
    bulkOperationRunQuery(query: $query) {
      bulkOperation { id status objectCount url }
      userErrors { field message }
    }
  }
"""

BULK_STATUS_QUERY = """
  query BulkStatus {
    currentBulkOperation {
      id status errorCode objectCount url finishedAt
    }
  }
"""

# Full-catalog bulk query for stores over ~1,000 products (per docs:
# Bulk Operations don't draw on the live point bucket, max 5 concurrent).
PRODUCTS_BULK_QUERY = """
{
  products {
    edges {
      node {
        id
        title
        handle
        descriptionHtml
        vendor
        productType
        tags
        status
        updatedAt
        seo { title description }
        priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
        images(first: 100) { nodes { url altText position } }
        metafields(first: 50) { nodes { namespace key type value } }
        variants(first: 100) { nodes {
          id title sku barcode position price { amount } currentlyNotAvailable
        } }
      }
    }
  }
}
"""
