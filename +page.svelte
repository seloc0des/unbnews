<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	type StoryLink = { source: string; url: string; bias: 'L' | 'C' | 'R' };
	type Story = {
		headline: string;
		brief: string;
		spectrum: { L: number; C: number; R: number };
		total_sources: number;
		blindspot: string | null;
		links: StoryLink[];
	};

	let stories: Story[] = [];
	let ageMins = 0;
	let loading = true;
	let refreshing = false;
	let errorMsg = '';
	let filterMode: 'all' | 'balanced' | 'blindspot' = 'all';
	let topicQuery = '';

	async function loadFeed(forceRefresh = false) {
		if (forceRefresh) refreshing = true;
		else loading = true;
		errorMsg = '';
		try {
			const res = await fetch(`/api/v1/news/feed${forceRefresh ? '?refresh=true' : ''}`, {
				credentials: 'include'
			});
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = await res.json();
			stories = data.stories ?? [];
			ageMins = data.age_mins ?? 0;
		} catch (err) {
			errorMsg = 'Could not load news feed. Check backend logs or RSS connectivity.';
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	onMount(() => {
		loadFeed(false);
	});

	$: filteredStories = stories.filter((s) => {
		const isBalanced = s.spectrum.L > 0 && s.spectrum.R > 0;
		if (filterMode === 'balanced' && !isBalanced) return false;
		if (filterMode === 'blindspot' && !s.blindspot) return false;
		if (topicQuery.trim()) {
			const q = topicQuery.trim().toLowerCase();
			return s.headline.toLowerCase().includes(q) || s.brief.toLowerCase().includes(q);
		}
		return true;
	});

	function pct(val: number, total: number): number {
		return Math.round((val / Math.max(1, total)) * 100);
	}

	function askInChat(headline: string) {
		goto(`/?q=${encodeURIComponent(`Explain the different perspectives on: ${headline}`)}`);
	}
</script>

<div class="flex flex-col w-full h-full overflow-y-auto px-5 py-6 md:px-10 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100">
	<div class="max-w-5xl w-full mx-auto">
		<!-- Header -->
		<div class="flex flex-wrap items-center justify-between gap-3 pb-4 mb-5 border-b border-gray-200 dark:border-gray-800">
			<div>
				<h1 class="text-xl font-semibold tracking-tight">Unbiased News Desk</h1>
				<p class="text-xs text-gray-500 dark:text-gray-400">
					Updated {ageMins}m ago &middot; {filteredStories.length} stories &middot; Click any publisher to read the full article
				</p>
			</div>

			<div class="flex flex-wrap items-center gap-2">
				<input
					type="text"
					bind:value={topicQuery}
					placeholder="Filter topics..."
					class="px-3 py-1.5 text-xs rounded-full border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-850 focus:outline-none focus:ring-1 focus:ring-blue-500"
				/>
				<button
					class="px-3 py-1.5 text-xs font-medium rounded-full border transition {filterMode === 'all' ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900 border-transparent' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300'}"
					on:click={() => (filterMode = 'all')}
				>
					All
				</button>
				<button
					class="px-3 py-1.5 text-xs font-medium rounded-full border transition {filterMode === 'balanced' ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900 border-transparent' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300'}"
					on:click={() => (filterMode = 'balanced')}
				>
					Multi-wing
				</button>
				<button
					class="px-3 py-1.5 text-xs font-medium rounded-full border transition {filterMode === 'blindspot' ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900 border-transparent' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300'}"
					on:click={() => (filterMode = 'blindspot')}
				>
					Blindspots
				</button>
				<button
					class="px-3 py-1.5 text-xs font-medium rounded-full border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
					disabled={refreshing}
					on:click={() => loadFeed(true)}
				>
					{refreshing ? 'Refreshing...' : '↻ Refresh'}
				</button>
			</div>
		</div>

		{#if loading}
			<div class="py-16 text-center text-sm text-gray-500 dark:text-gray-400">
				Loading multi-spectrum news clusters...
			</div>
		{:else if errorMsg}
			<div class="py-12 text-center text-sm text-amber-600 dark:text-amber-400">
				{errorMsg}
			</div>
		{:else if filteredStories.length === 0}
			<div class="py-12 text-center text-sm text-gray-500 dark:text-gray-400">
				No matching stories for this filter.
			</div>
		{:else}
			<div class="grid grid-cols-1 md:grid-cols-2 gap-3.5">
				{#each filteredStories as story, idx}
					<article class="flex flex-col rounded-2xl border border-gray-200 dark:border-gray-800 bg-gray-50/70 dark:bg-gray-850 p-4 {idx === 0 && filteredStories.length > 2 ? 'md:col-span-2 p-5' : ''}">
						<div class="flex items-center justify-between gap-2 mb-1.5">
							<span class="text-xs font-medium text-gray-500 dark:text-gray-400">
								{story.total_sources} {story.total_sources === 1 ? 'source' : 'sources'}
							</span>
							{#if story.blindspot}
								<span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400">
									{story.blindspot}
								</span>
							{:else if story.spectrum.L > 0 && story.spectrum.R > 0}
								<span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
									Multi-wing
								</span>
							{/if}
						</div>

						<h2 class="font-semibold tracking-tight leading-snug mb-1.5 {idx === 0 && filteredStories.length > 2 ? 'text-lg' : 'text-[15px]'}">
							{story.headline}
						</h2>

						<p class="text-[13.5px] text-gray-600 dark:text-gray-300 leading-relaxed mb-3 flex-grow">
							{story.brief}
						</p>

						<!-- 3-Wing Spectrum Bar -->
						<div class="flex h-1 w-full rounded-full overflow-hidden bg-gray-200 dark:bg-gray-700 mb-1.5">
							<span class="bg-blue-500" style="width:{pct(story.spectrum.L, story.total_sources)}%"></span>
							<span class="bg-gray-500" style="width:{pct(story.spectrum.C, story.total_sources)}%"></span>
							<span class="bg-amber-500" style="width:{pct(story.spectrum.R, story.total_sources)}%"></span>
						</div>
						<div class="flex gap-3 text-[11.5px] text-gray-500 dark:text-gray-400 mb-3 tabular-nums">
							<span class="inline-flex items-center gap-1"><i class="w-1.5 h-1.5 rounded-full bg-blue-500"></i>L {story.spectrum.L}</span>
							<span class="inline-flex items-center gap-1"><i class="w-1.5 h-1.5 rounded-full bg-gray-500"></i>C {story.spectrum.C}</span>
							<span class="inline-flex items-center gap-1"><i class="w-1.5 h-1.5 rounded-full bg-amber-500"></i>R {story.spectrum.R}</span>
						</div>

						<!-- Direct Publisher Click-Out Pills -->
						<div class="flex flex-wrap items-center justify-between gap-2 pt-2.5 border-t border-gray-200/80 dark:border-gray-800">
							<div class="flex flex-wrap gap-1.5">
								{#each story.links as link}
									<a
										href={link.url}
										target="_blank"
										rel="noopener noreferrer"
										class="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-full border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:border-blue-500 transition"
									>
										<span class="w-1.5 h-1.5 rounded-full {link.bias === 'L' ? 'bg-blue-500' : link.bias === 'R' ? 'bg-amber-500' : 'bg-gray-500'}"></span>
										<span>{link.source}</span>
										<span class="text-gray-400">&#8599;</span>
									</a>
								{/each}
							</div>
							<button
								type="button"
								on:click={() => askInChat(story.headline)}
								class="px-2.5 py-1 text-xs font-medium rounded-full bg-blue-500/10 text-blue-600 dark:text-blue-400 hover:bg-blue-500/20 transition"
							>
								Ask AI &#10022;
							</button>
						</div>
					</article>
				{/each}
			</div>
		{/if}
	</div>
</div>
