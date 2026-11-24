<!-- Add this clickable News tab link inside src/lib/components/layout/Sidebar.svelte alongside /notes and /workspace -->
<div class="px-1.5 flex justify-center text-gray-800 dark:text-gray-200">
	<a
		class="grow flex items-center space-x-3 rounded-lg px-2.5 py-2 hover:bg-gray-100 dark:hover:bg-gray-900 transition"
		href="/news"
		on:click={() => {
			if ($mobile) {
				showSidebar.set(false);
			}
		}}
		draggable="false"
	>
		<div class="self-center">
			<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="size-4.5">
				<path stroke-linecap="round" stroke-linejoin="round" d="M12 7.5h1.5m-1.5 3h1.5m-7.5 3h7.5m-7.5 3h7.5m3-9h3.375c.621 0 1.125.504 1.125 1.125V18a2.25 2.25 0 0 1-2.25 2.25M16.5 7.5V18a2.25 2.25 0 0 0 2.25 2.25M16.5 7.5V4.875c0-.621-.504-1.125-1.125-1.125H4.125C3.504 3.75 3 4.254 3 4.875V18a2.25 2.25 0 0 0 2.25 2.25h13.5" />
			</svg>
		</div>
		<div class="flex self-center translate-y-[0.5px]">
			<div class="self-center font-medium text-sm font-primary">News</div>
		</div>
	</a>
</div>
