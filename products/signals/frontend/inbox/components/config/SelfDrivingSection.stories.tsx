import type { Meta, StoryObj } from '@storybook/react'

import { useStorybookMocks } from '~/mocks/browser'

import { mockAutonomy, mockTeamConfig } from '../../__mocks__/inboxMocks'
import { SelfDrivingSection } from './SelfDrivingSection'

// The "PR generation" card from the agents setup rail. It reads the team-wide default threshold
// (`signals/config`) and the current user's personal override (`users/@me/signal_autonomy`), so both
// GETs are mocked per story to place the two threshold controls in a given state.

interface CardState {
    /** `autostart_enabled` on the team config – the master switch. */
    enabled: boolean
    /** Team-wide default threshold, always a concrete priority. */
    projectThreshold: string
    /** Personal override, or null to inherit the project threshold ("Default"). */
    myThreshold: string | null
}

function Card({ enabled, projectThreshold, myThreshold }: CardState): JSX.Element {
    useStorybookMocks({
        get: {
            '/api/projects/:id/signals/config': {
                ...mockTeamConfig,
                autostart_enabled: enabled,
                default_autostart_priority: projectThreshold,
            },
            '/api/users/@me/signal_autonomy': { ...mockAutonomy, autostart_priority: myThreshold },
        },
    })
    // Mimic the agents rail (`w-80` aside + the column's `px-4 py-3`) so the card lays out as in the scene.
    return (
        <div className="w-80 px-4 py-3 bg-surface-secondary">
            <SelfDrivingSection />
        </div>
    )
}

const meta: Meta = {
    title: 'Scenes-App/Inbox/PRGeneration',
    component: SelfDrivingSection,
    parameters: {
        layout: 'centered',
        viewMode: 'story',
        mockDate: '2024-03-20',
    },
}
export default meta

type Story = StoryObj

// Personal override set below the project default: "My threshold" reads P1+ while the project stays P2+.
export const PersonalOverride: Story = {
    render: () => <Card enabled projectThreshold="P2" myThreshold="P1" />,
}

// No personal override: "My threshold" shows "Default" and inherits the project threshold.
export const PersonalDefault: Story = {
    render: () => <Card enabled projectThreshold="P2" myThreshold={null} />,
}

// Master switch off: both thresholds are hidden and only the reassurance copy shows.
export const Disabled: Story = {
    render: () => <Card enabled={false} projectThreshold="P2" myThreshold={null} />,
}
