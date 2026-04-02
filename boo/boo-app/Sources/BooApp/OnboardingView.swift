import SwiftUI
import BooCore

/// Onboarding wizard — 4 screens: Setup, Demo, Remote, Ready.
struct OnboardingView: View {
    @Environment(AppState.self) private var appState
    @State private var coordinator: OnboardingCoordinator?
    let onComplete: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let coordinator {
                // Step dots
                HStack(spacing: 8) {
                    ForEach(0..<OnboardingCoordinator.Step.allCases.count, id: \.self) { i in
                        Circle()
                            .fill(i <= coordinator.currentStep.rawValue ? Color.accentColor : Color.secondary.opacity(0.3))
                            .frame(width: 8, height: 8)
                    }
                }
                .padding(.top, 20)
                .padding(.bottom, 12)

                // Screen content
                screenContent(coordinator: coordinator)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: 520, height: 460)
        .onAppear {
            coordinator = OnboardingCoordinator(appState: appState)
        }
    }

    @ViewBuilder
    private func screenContent(coordinator: OnboardingCoordinator) -> some View {
        switch coordinator.currentStep {
        case .boot:
            BootSequenceView(coordinator: coordinator)

        case .firstContact:
            FirstContactView(coordinator: coordinator)
                .environment(appState)

        case .remoteLink:
            RemoteLinkView(coordinator: coordinator)
                .environment(appState)

        case .ready:
            MissionReadyView(coordinator: coordinator, onComplete: onComplete)
                .environment(appState)
        }
    }
}
