const GROUP_BY_DOMAIN = new Map([
  ["exxxoticaexpo.com", "vision"],
  ["alphastyle.store", "vision"],
  ["laxton.se", "vision"],
  ["goreport.com", "vision"],
  ["pjclarkes.com", "vision"],
  ["shorelinedentalstudio.com", "vision"],
  ["smashhouseburgers.com", "vision"],
  ["19cleveland.com", "vertex"],
  ["effyscafe.com", "vertex"],
  ["grillpointnyc.com", "vertex"],
  ["hearwellservices.com", "vertex"],
  ["nychearing.com", "vertex"],
  ["nysapphire.com", "vertex"],
  ["shawarma-mia.com", "vertex"],
]);

export const CLIENT_GROUPS = ["all", "vision", "vertex"];

export function clientGroup(client) {
  const domain = String(client?.domain || "").toLowerCase().replace(/^www\./, "");
  return GROUP_BY_DOMAIN.get(domain) || null;
}

export function filterByClientGroup(clients, group) {
  if (!group || group === "all") return clients;
  return clients.filter((client) => clientGroup(client) === group);
}
