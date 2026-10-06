// Tier board on a list page: drag items between tiers, move them from a menu,
// and edit the tiers. Bind once: this script is re-evaluated on boosted
// (hx:boost) navigation.
if (!window.__floppyListTiersBound) {
  window.__floppyListTiersBound = true;

  const newTierId = () => `t${Math.random().toString(36).slice(2, 10)}`;
  const COLORS = [
    "#ff7f7f",
    "#ffbf7f",
    "#ffdf7f",
    "#bfff7f",
    "#7fbfff",
    "#c4a7ff",
    "#ff9fd4",
    "#aab2bd",
  ];

  document.addEventListener("alpine:init", () => {
    Alpine.data("tierBoard", (config) => ({
      ...config,
      colors: COLORS,
      sortables: [],
      lastDrag: 0,
      error: "",
      moving: null,
      editing: null,
      unrankedCount: 0,

      init() {
        this.refreshCount();
        if (!this.canEdit || typeof Sortable === "undefined") return;
        this.$root.querySelectorAll(".tier-drop").forEach((zone) => {
          this.sortables.push(
            Sortable.create(zone, {
              group: "tier-tiles",
              animation: 150,
              draggable: ".tier-tile",
              ghostClass: "opacity-40",
              delay: 150,
              delayOnTouchOnly: true,
              touchStartThreshold: 5,
              onEnd: (event) => {
                this.lastDrag = Date.now();
                this.save(event.item.dataset.itemId, event.to);
              },
            }),
          );
        });
      },

      destroy() {
        this.sortables.forEach((sortable) => sortable.destroy());
        this.sortables = [];
      },

      refreshCount() {
        this.unrankedCount = this.$root.querySelectorAll(
          '.tier-drop[data-tier=""] .tier-tile',
        ).length;
      },

      async post(url, body, headers = {}) {
        const response = await fetch(url, {
          method: "POST",
          headers: { "X-CSRFToken": this.csrfToken, ...headers },
          body,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response;
      },

      // Tell the server which tier the item is in and the tier's new order.
      async save(itemId, zone) {
        const params = new URLSearchParams({
          item_id: itemId,
          tier: zone.dataset.tier,
        });
        zone.querySelectorAll(".tier-tile").forEach((tile) => {
          params.append("item_ids[]", tile.dataset.itemId);
        });
        this.refreshCount();
        try {
          await this.post(this.moveUrl, params);
        } catch {
          window.location.reload();
        }
      },

      openMove(itemId, title, tierId) {
        if (Date.now() - this.lastDrag < 300) return;
        this.moving = { itemId, title, tierId };
      },

      moveTo(tierId) {
        const { itemId } = this.moving;
        this.moving = null;
        const tile = this.$root.querySelector(`.tier-tile[data-item-id="${itemId}"]`);
        const zone = this.$root.querySelector(`.tier-drop[data-tier="${tierId}"]`);
        if (!tile || !zone) return;
        zone.appendChild(tile);
        this.save(itemId, zone);
      },

      editTier(tierId) {
        const tier = this.tiers.find((entry) => entry.id === tierId);
        this.error = "";
        this.editing = { ...tier, isNew: false };
      },

      addTier() {
        if (this.tiers.length >= this.maxTiers) return;
        this.error = "";
        this.editing = {
          id: newTierId(),
          name: "",
          color: COLORS[this.tiers.length % COLORS.length],
          isNew: true,
        };
      },

      async saveTiers(tiers) {
        try {
          await this.post(
            this.saveUrl,
            JSON.stringify({ tiers }),
            { "Content-Type": "application/json" },
          );
          window.location.reload();
        } catch {
          this.error = "Could not save the tiers.";
        }
      },

      saveEditing() {
        const draft = { id: this.editing.id, name: this.editing.name.trim(), color: this.editing.color };
        if (!draft.name) {
          this.error = "Give the tier a name.";
          return;
        }
        const tiers = this.tiers.map((entry) => ({ id: entry.id, name: entry.name, color: entry.color }));
        const index = tiers.findIndex((entry) => entry.id === draft.id);
        if (index === -1) tiers.push(draft);
        else tiers[index] = draft;
        this.saveTiers(tiers);
      },

      shiftEditing(offset) {
        const tiers = this.tiers.map((entry) => ({ id: entry.id, name: entry.name, color: entry.color }));
        const index = tiers.findIndex((entry) => entry.id === this.editing.id);
        const target = index + offset;
        if (index === -1 || target < 0 || target >= tiers.length) return;
        [tiers[index], tiers[target]] = [tiers[target], tiers[index]];
        this.saveTiers(tiers);
      },

      deleteEditing() {
        const tiers = this.tiers
          .filter((entry) => entry.id !== this.editing.id)
          .map((entry) => ({ id: entry.id, name: entry.name, color: entry.color }));
        if (!tiers.length) {
          this.error = "Keep at least one tier.";
          return;
        }
        this.saveTiers(tiers);
      },
    }));
  });
}
